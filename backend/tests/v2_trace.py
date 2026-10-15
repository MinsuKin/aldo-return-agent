"""Test helper: make sure the demo v2 A2A event types exist while the domain stream finishes them.

The single-chat tests emit and read ``handoff_to_returns``, ``handback``, ``reviews_shared``,
``delivered`` and ``memory_consulted`` on the owner's agent-to-agent stream. ``returns_domain.trace``
(another stream's file) allowlists them. ``ensure_v2_event_types`` adds only what is still missing,
for the duration of one test, with the same public fields the addendum defines, and does nothing
when ``trace`` already knows a type. Once the domain stream has everything the helper is a no-op.
"""

from __future__ import annotations

import pytest

from returns_domain import trace

#: type -> public fields (PIVOT_ADDENDUM 11.4 and 12.3).
WANTED: dict[str, frozenset[str]] = {
    "reviews_shared": frozenset({"style_id", "product_name", "n_reviews", "n_flagged", "flagged_review_ids", "provenance"}),
    "handoff_to_returns": frozenset({"order_id", "product_name", "size", "color", "mode", "to_agent"}),
    "handback": frozenset({"order_id", "outcome", "transaction_id", "case_id", "mode"}),
    "delivered": frozenset({"order_id", "delivered_at", "product_name", "size"}),
    "memory_consulted": frozenset({"n_entries", "fields"}),
}


def missing_types() -> list[str]:
    return [name for name in WANTED if name not in trace.A2A_PUBLIC_FIELDS or name not in trace.EVENT_TYPES]


def ensure_v2_event_types(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Add the missing types to ``trace`` for this test; returns the names that had to be added."""
    added = missing_types()
    if not added:
        return []
    types = tuple(trace.EVENT_TYPES) + tuple(name for name in added if name not in trace.EVENT_TYPES)
    fields = dict(trace.A2A_PUBLIC_FIELDS)
    for name in added:
        fields.setdefault(name, WANTED[name])
    monkeypatch.setattr(trace, "EVENT_TYPES", types)
    monkeypatch.setattr(trace, "A2A_PUBLIC_FIELDS", fields)
    return added
