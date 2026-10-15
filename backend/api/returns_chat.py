"""The ALDO side of the single chat: after a hand-off the owner's messages go to the return workflow.

PIVOT_ADDENDUM 11.1 D3, 11.5, 12.1, 12.3. The owner talks to ALDO bot only. ALDO bot's purchase
workflow answers while the conversation is in mode ``bot``; its ``hand_off_to_returns`` tool
switches the personal-agent conversation to mode ``returns`` (with the order id, the owner's
triggering message and a trusted memory context). From then on each owner message is routed
straight to a return-workflow run, with no second model hop, until the return completes or is
escalated (the run reports ``transaction_created`` or ``case_created``) or the owner cancels; then
the mode goes back to ``bot`` and the A2A stream shows ``handback``.

This module is the composition-root glue between the two tables, so it may read both:

* ``ReturnsChat.forward``: create or continue the ALDO-side conversation of the hand-off (it
  remembers the personal-agent conversation in ``pa_conv_id``), forward the stored triggering text
  once and then each new message, and start the run through the normal conversations and runs
  machinery (one active run per conversation, lease, queued run, worker hand-off).
* ``hand_back`` / ``settle_after_run``: the way back (used by the router for "cancel" and by the
  worker after a run).
* ``recent_orders``: the owner's recent ALDO orders in the compact form the bot's prompt, its
  hand-off tool and the memory context use.

Nothing here puts a model-written value into a payload: the order id, the conversation ids and
the memory context all come from the personal-agent table, which only trusted code writes.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from returns_domain import config
from returns_domain.errors import DomainError, ErrorCode

from . import providers
from .http import logger
from .routes_returns import start_customer_run

conversations = providers.lazy("conversations")
orders = providers.lazy("orders")
store = providers.lazy("store")
runs = providers.lazy("runs")
trace = providers.lazy("trace")

MODE_BOT = "bot"
MODE_RETURNS = "returns"
#: A message that starts with one of these ends the return and brings the owner back to the bot.
CANCEL_PREFIXES = ("cancel", "back to bot")
#: Events of a return run that mean "this return is done" (a quote is not: the owner still has to approve).
HANDBACK_EVENTS = ("transaction_created", "case_created")
ACTOR_RETURN = "aldo_agent"
RECENT_ORDERS = 6
MAX_LINES_PER_ORDER = 12


def is_cancel(text: str) -> bool:
    """True when the owner's message starts with ``cancel`` or ``back to bot`` (case-insensitive)."""
    return isinstance(text, str) and text.strip().lower().startswith(CANCEL_PREFIXES)


def forward_text(conversation: Mapping[str, Any], text: str) -> str:
    """The text for the return run: the stored triggering message ONCE, then each new message.

    The first message after the hand-off is the owner's triggering text followed by the new one
    (the new one is usually the answer to the bot's question), so the return workflow starts with
    the whole request. It never exceeds the message limit: the stored text gives way, not the
    owner's new message.
    """
    stored = conversation.get("handoff_text")
    if conversation.get("handoff_forwarded") or not isinstance(stored, str) or not stored.strip():
        return text
    room = config.get_settings().max_message_chars - len(text) - 1
    if room < 1:
        return text
    return f"{stored.strip()[:room].rstrip()}\n{text}"


def recent_orders(customer_id: str, limit: int = RECENT_ORDERS) -> list[dict[str, Any]]:
    """The owner's recent ALDO orders, newest first, compact: what the bot needs to pick an order.

    ``order_id``, ``channel`` (``agent`` for orders ALDO bot's purchases created), ``ordered_at``,
    ``delivered_at`` and the lines (``product_name``, ``color``, ``size``, ``qty``).
    """
    compact: list[dict[str, Any]] = []
    for order in list(orders.list_orders(customer_id))[:limit]:
        compact.append(
            {
                "order_id": order.order_id,
                "channel": order.channel,
                "ordered_at": order.ordered_at,
                "delivered_at": order.delivered_at,
                "lines": [
                    {
                        "product_name": line.product_name,
                        "color": line.color,
                        "size": line.size,
                        "qty": line.purchased_qty,
                    }
                    for line in order.lines[:MAX_LINES_PER_ORDER]
                ],
            }
        )
    return compact


def _memory() -> Any:
    memory = providers.optional("personal_agent.memory")
    if memory is None:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "ALDO bot is not available.")
    return memory


def hand_back(
    customer_id: str,
    pa_conv: str,
    *,
    reason: str,
    expected_order_id: str | None = None,
    repo: Any = None,
    ids: Mapping[str, Any] | None = None,
) -> bool:
    """Return the conversation to mode ``bot`` and show ``handback`` on the A2A stream.

    True only for the caller that changed the mode, so a late or repeated hand-back (a replayed
    approval, a double "cancel") emits nothing. ``reason`` is the public ``outcome`` of the event:
    ``transaction_created``, ``case_created`` or ``owner_cancelled``.
    """
    repo = repo or _memory().get_repo()
    previous = repo.set_bot_mode(customer_id, pa_conv, expected_order_id=expected_order_id)
    if previous is None:
        return False
    public = {key: value for key, value in (ids or {}).items() if isinstance(value, str) and value}
    order_id = previous.get("returns_order_id")
    trace.emit_a2a_safe(
        customer_id,
        "handback",
        {**({"order_id": order_id} if order_id else {}), "outcome": reason, "mode": MODE_BOT, **public},
        actor=ACTOR_RETURN,
    )
    return True


def settle_after_run(customer_id: str, aldo_conv_id: str, run_id: str) -> bool:
    """After a return-workflow run: hand the chat back to the bot when the run completed the return.

    The ALDO-side conversation knows its chat (``pa_conv_id``, written when the hand-off created
    it). A run that created a transaction or a case ends the return. Never raises: this runs in
    the worker after the run, and a failure here must not change the run's result.
    """
    try:
        conv = store.get_item(store.conv_key(aldo_conv_id)) or {}
        pa_conv = conv.get("pa_conv_id")
        if not isinstance(pa_conv, str) or not pa_conv:
            return False
        events = trace.list_events(run_id, 0, 500).events
        found = next((e for e in events if e.get("type") in HANDBACK_EVENTS), None)
        if found is None:
            return False
        payload = found.get("payload") or {}
        ids = {"transaction_id": payload.get("transaction_id"), "case_id": payload.get("case_id")}
        return hand_back(customer_id, pa_conv, reason=found["type"], ids=ids)
    except Exception:
        logger.exception("handback_check_failed", run_id=run_id)
        return False


class ReturnsChat:
    """Routes the owner's messages of a hand-off to the return workflow."""

    def __init__(self, enqueue: Callable[[dict[str, Any]], None], repo_provider: Callable[[], Any]) -> None:
        self._enqueue = enqueue
        self._repo_provider = repo_provider

    # ------------------------------------------------------------------------ guards

    def return_run_active(self, conversation: Mapping[str, Any]) -> bool:
        """True while the hand-off's ALDO-side conversation has a run in progress (its lease)."""
        returns_conv = conversation.get("returns_conv_id")
        if not returns_conv:
            return False
        try:
            return bool(runs.lease_active(returns_conv))
        except DomainError:
            return False

    # ---------------------------------------------------------------------- forwarding

    def forward(self, owner: str, pa_conv: str, conversation: Mapping[str, Any], text: str) -> str:
        """Start a return-workflow run for the owner's text and return its run id.

        The ALDO-side conversation is created on the first message (an order that is not the
        owner's hands the chat back to the bot and is refused), the stored triggering text goes
        along once, and the run is the normal customer run of the order (RUN_IN_PROGRESS while
        another one is active). The payload carries the trusted server fields ``memory_context``
        and ``selected_order_id`` (the hand-off's order, the same value as ``order_id``). When the
        bot named no order both are absent or null and the return workflow looks the owner's
        recent delivered orders up itself.
        """
        repo = self._repo_provider()
        order_id = conversation.get("returns_order_id") or None  # None: the bot named no order
        returns_conv = self._ensure_conversation(owner, pa_conv, conversation, repo)
        message = forward_text(conversation, text)
        context = conversation.get("memory_context") or self._fresh_context(owner, repo)
        run_id = start_customer_run(
            self._enqueue,
            customer_id=owner,
            conv_id=returns_conv,
            kind="customer",
            order_id=order_id,
            message=message,
            memory_context=context,
            selected_order_id=order_id,
        )
        repo.mark_handoff_forwarded(owner, pa_conv)
        return run_id

    def cancel(self, owner: str, pa_conv: str, conversation: Mapping[str, Any]) -> bool:
        """The owner left the return: back to the bot, ``handback`` on the A2A stream."""
        return hand_back(
            owner,
            pa_conv,
            reason="owner_cancelled",
            expected_order_id=conversation.get("returns_order_id"),
            repo=self._repo_provider(),
        )

    # ------------------------------------------------------------------------ internals

    def _ensure_conversation(
        self, owner: str, pa_conv: str, conversation: Mapping[str, Any], repo: Any
    ) -> str:
        existing = conversation.get("returns_conv_id")
        if existing:
            try:
                conversations.get_conversation(owner, existing)
                return existing
            except DomainError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                repo.forget_returns_conversation(owner, pa_conv, existing)  # wiped by a demo reset
        try:
            created = conversations.create_conversation(owner, conversation.get("returns_order_id") or None)
        except DomainError as exc:
            if exc.code in (ErrorCode.ORDER_NOT_FOUND, ErrorCode.NOT_AUTHORIZED):
                # Not an order of this owner: do not leave the chat stuck in a return that cannot start.
                hand_back(
                    owner, pa_conv, reason="order_unavailable",
                    expected_order_id=conversation.get("returns_order_id"), repo=repo,
                )
            raise
        store.update_item(store.conv_key(created), set={"pa_conv_id": pa_conv}, return_new=False)
        stored = repo.attach_returns_conversation(owner, pa_conv, created)
        if stored is None:  # the chat left returns mode while we were creating the conversation
            raise DomainError(
                ErrorCode.TRANSACTION_CONFLICT, "The conversation changed. Please send your message again."
            )
        return stored

    def _fresh_context(self, owner: str, repo: Any) -> dict[str, Any] | None:
        """A hand-off without a stored context (made by older code): build one from what is known now."""
        try:
            from personal_agent.context import build_memory_context

            return build_memory_context(repo.get_owner_memory(owner), self._orders_for(owner)) or None
        except Exception:
            logger.exception("memory_context_not_built")
            return None

    @staticmethod
    def _orders_for(owner: str) -> Sequence[Mapping[str, Any]]:
        try:
            return recent_orders(owner)
        except Exception:
            logger.exception("recent_orders_not_read")
            return []
