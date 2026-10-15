"""Small fakes shared by the API and worker tests (the provider seam makes them drop-in)."""

from __future__ import annotations

from typing import Any

from returns_domain.errors import DomainError


class GatewayError(DomainError):
    """Mirrors ``returns_domain.gateway.GatewayError``: ``extra`` is merged into ``to_dict``."""

    def __init__(self, code: str, message: str, *, extra: dict | None = None) -> None:
        super().__init__(code, message)
        self.extra = dict(extra or {})

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), **self.extra}


class FakeGateway:
    """Records every call and either answers or raises ``failure``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.failure: Exception | None = None

    def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.failure is not None:
            raise self.failure

    def size_assist(self, claims, request):
        self._call("size_assist", claims, request)
        return {"size_request_id": "SZR-3F9A1C", "style_id": "DEMO-STYLE-HERO", "sizes": []}

    def decide_size(self, claims, request):
        self._call("decide_size", claims, request)
        return {
            "size_request_id": request.get("size_request_id"),
            "status": "proposed",
            "awaiting": "owner_confirmation",
            "expires_at": "2026-10-16T16:15:00+00:00",
            "summary": {"size": request.get("chosen_size")},
        }

    def get_reviews(self, claims, request):
        self._call("get_reviews", claims, request)
        return {
            "style_id": request.get("style_id", "DEMO-STYLE-MEN001"),
            "product_name": "ALDO Men 001",
            "n_reviews": 1,
            "n_flagged": 0,
            "reviews": [
                {
                    "review_id": "DEMO-REV-MEN001-01",
                    "rating": 4,
                    "title": "Runs big",
                    "text": "Half a size big.",
                    "language": "en",
                    "verified_purchase": True,
                    "helpful_votes": 3,
                    "flags": [],
                }
            ],
            "provenance": "synthetic",
        }

    def get_proposal(self, customer_id, size_request_id):
        self._call("get_proposal", customer_id, size_request_id)
        return {"size_request_id": size_request_id, "status": "proposed", "price_cents": 12999}

    def confirm_purchase(self, customer_id, size_request_id, option_id=None):
        self._call("confirm_purchase", customer_id, size_request_id, option_id=option_id)
        return {"order_id": "DEMO-A2A-81B20E", "status": "placed_simulated", "replayed": False}

    def simulate_delivery(self, customer_id, order_id):
        self._call("simulate_delivery", customer_id, order_id=order_id)
        return {"order_id": order_id, "delivered_at": "2026-10-16"}

    def simulate_pickup(self, customer_id, transaction_id):
        self._call("simulate_pickup", customer_id, transaction_id=transaction_id)
        return {"transaction_id": transaction_id, "update_id": "MU-5C0A11"}

    def agent_card(self, base_url):
        self._call("agent_card", base_url)
        return {"name": "ALDO bot", "url": f"{base_url}/api/gateway"}

    def flush_outbox(self, customer_id=None, limit=10, client=None):
        self._call("flush_outbox", customer_id, limit=limit)
        return 0
