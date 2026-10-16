"""Request bodies for the browser-facing routes, validated by hand (``model_validate``) so errors
keep the ``{error:{...}}`` shape.

Unknown keys are ignored: requirement 1.2 says a customer identifier in a body must be ignored,
never trusted, so a stray ``customer_id`` is harmless on these session routes.

The agent-facing gateway routes have no models here on purpose. The personal agent is an
untrusted peer (PIVOT_ADDENDUM C3/C7), and ``returns_domain.gateway`` owns the strict
validation of its bodies (``extra="forbid"``, enumerations, length caps); this layer only parses
the JSON object and hands it over, so the schema has exactly one source of truth.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Variant = Literal["default", "dc_stockout", "all_stockout"]


class _SessionBody(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class SessionBody(_SessionBody):
    customer_id: str = Field(min_length=1, max_length=128)


class ResetBody(_SessionBody):
    variant: Variant = "default"


class DataPlatformBody(_SessionBody):
    """Admin: which source the fit insights come from (see docs/data-platform/DESIGN.md)."""

    mode: Literal["static", "platform", "shadow"]


class ConversationBody(_SessionBody):
    order_id: str | None = Field(default=None, min_length=1, max_length=128)


class MessageBody(_SessionBody):
    # The product limit (MAX_MESSAGE_CHARS, default 1000) is enforced by the route from
    # settings; this cap only bounds what is parsed at all.
    text: str = Field(min_length=1, max_length=10_000)
    order_id: str | None = Field(default=None, min_length=1, max_length=128)


class RequoteBody(_SessionBody):
    quote_id: str = Field(min_length=1, max_length=128)


class ApproveBody(_SessionBody):
    option_id: str = Field(min_length=1, max_length=64)
    payload_hash: str = Field(min_length=1, max_length=128)


class ConfirmPurchaseBody(_SessionBody):
    """``POST /purchases/<id>/confirm``: the id of the option the owner reviewed (optional)."""

    option_id: str | None = Field(default=None, min_length=1, max_length=64)


class SimulateDeliveryBody(_SessionBody):
    order_id: str = Field(min_length=1, max_length=128)


class SimulatePickupBody(_SessionBody):
    transaction_id: str = Field(min_length=1, max_length=128)
