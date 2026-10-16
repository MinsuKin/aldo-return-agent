"""ALDO Fit Gateway routes (PIVOT_ADDENDUM 5.6, 5.7, 10.1, C4, C7; contracts.md C.8 and F.3/F.4).

Two token families, never mixed:

* ``/gateway/*`` accepts ONLY delegated tokens (the personal agent's short-lived, capped token). A
  session token is NOT_AUTHORIZED there. The calls are deterministic gateway functions: a
  size assist returns ALDO's own facts, the reviews are untrusted customer text with flags, a
  size decision only RECORDS a proposal.
* ``/purchases/*`` accepts ONLY session tokens: the owner's click is what turns a proposal into
  an order, exactly like the return approval. A delegated token is NOT_AUTHORIZED there, so no
  agent can place an order.

The agent card routes are public. ``/a2a/events`` and the demo simulate controls use the normal
session token; the gateway functions check that the order or transaction belongs to the
signed-in customer. The agent's body goes to the gateway module untouched: it owns the strict
schema (extra fields refused, enumerations, length caps).
"""

from __future__ import annotations

from typing import Any

from . import providers
from .http import DELEGATED, PUBLIC, SESSION, Api, Request, logger
from .models import ConfirmPurchaseBody, SimulateDeliveryBody, SimulatePickupBody
from .routes_returns import event_page

gateway = providers.lazy("gateway")

AGENT_CARD_PATHS = ("/gateway/.well-known/agent-card.json", "/.well-known/agent-card.json")


def register(api: Api) -> None:
    # ----------------------------------------------------- agent-facing (delegated token)

    @api.post("/gateway/size-assist", auth=DELEGATED)
    def size_assist(req: Request) -> Any:
        result = gateway.size_assist(req.claims, req.body_dict())
        _annotate_size_request(req, result)
        return result

    @api.post("/gateway/size-decision", auth=DELEGATED)
    def size_decision(req: Request) -> Any:
        """Records a proposal (``status: proposed``, awaiting the owner); no order, no stock."""
        body = req.body_dict()
        if isinstance(body.get("size_request_id"), str):
            req.annotate(size_request_id=body["size_request_id"][:32])
        return gateway.decide_size(req.claims, body)

    @api.post("/gateway/reviews", auth=DELEGATED)
    def reviews(req: Request) -> Any:
        """Customer reviews of one style: untrusted text with a ``flags`` list per review.

        Flagged (instruction-like) reviews are served, not dropped: the caller must ignore them.
        """
        body = req.body_dict()
        if isinstance(body.get("style_id"), str):
            req.annotate(style_id=body["style_id"][:64])
        return gateway.get_reviews(req.claims, body)

    # ------------------------------------------------------- owner-facing (session token)

    @api.get("/purchases/<size_request_id>", auth=SESSION)
    def get_purchase(req: Request, size_request_id: str) -> Any:
        """The server-rendered proposal the owner reviews before confirming."""
        req.annotate(size_request_id=size_request_id)
        return gateway.get_proposal(req.need_customer(), size_request_id)

    @api.post("/purchases/<size_request_id>/confirm", auth=SESSION)
    def confirm_purchase(req: Request, size_request_id: str) -> Any:
        """The owner's click: the one transaction that creates the simulated order."""
        customer_id = req.need_customer()
        body = req.body_model(ConfirmPurchaseBody, optional=True)
        req.annotate(size_request_id=size_request_id)
        return gateway.confirm_purchase(customer_id, size_request_id, option_id=body.option_id)

    # ------------------------------------------------------------------ agent card (public)

    def agent_card(req: Request) -> Any:
        return gateway.agent_card(public_base_url(req))

    for path in AGENT_CARD_PATHS:
        api.get(path, auth=PUBLIC)(agent_card)

    # ------------------------------------------------- trace and demo controls (session token)

    @api.get("/a2a/events", auth=SESSION)
    def a2a_events(req: Request) -> dict[str, Any]:
        customer_id = req.need_customer()
        after = req.query_int("after")
        return event_page(_read_a2a_events(customer_id, after))

    @api.post("/demo/simulate-delivery", auth=SESSION)
    def simulate_delivery(req: Request) -> Any:
        body = req.body_model(SimulateDeliveryBody)
        return gateway.simulate_delivery(req.need_customer(), order_id=body.order_id)

    @api.post("/demo/simulate-pickup", auth=SESSION)
    def simulate_pickup(req: Request) -> Any:
        body = req.body_model(SimulatePickupBody)
        # The gateway queues the non-provisional write-back and flushes it itself.
        return gateway.simulate_pickup(req.need_customer(), transaction_id=body.transaction_id)


def public_base_url(req: Request) -> str:
    """The origin the outside world uses to reach this API, without a trailing slash.

    ``GATEWAY_BASE_URL``, else the SSM parameter ``/aldo-demo/gateway-url`` (cached per process).
    Behind CloudFront the Lambda sees the execute-api Host header, never the public one, so the
    request is only a last resort for runs where neither is configured (local dev, tests).
    """
    try:
        return providers.module("personal_agent_client").resolve_base_url()
    except Exception:
        logger.warning("gateway_base_url_not_configured_using_request_host")
        return req.request_origin()


def _read_a2a_events(customer_id: str, after: int) -> Any:
    """``trace.list_a2a_events`` (``read_a2a`` is its older alias)."""
    module = providers.module("trace")
    reader = getattr(module, "list_a2a_events", None) or module.read_a2a
    return reader(customer_id, after)


def _annotate_size_request(req: Request, result: Any) -> None:
    size_request_id = result.get("size_request_id") if isinstance(result, dict) else getattr(
        result, "size_request_id", None
    )
    if size_request_id:
        req.annotate(size_request_id=size_request_id)
