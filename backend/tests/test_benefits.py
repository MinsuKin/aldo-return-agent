"""benefits.py: the member tier, the monthly perk ledger and the reset (domain v2 phase 2).

Runs against the shipped fixtures (David is platinum, John silver, Ava has no tier).
"""

from __future__ import annotations

import pytest

from returns_domain import benefits, fixtures, policy, store
from returns_domain.errors import DomainError
from tests.bundle_world import DAVID, JOHN, PERK_OCTOBER, NEXT_MONTH_NOW, Bundles, keys, ledger
from tests.conftest import AVA, Flow

pytestmark = pytest.mark.usefixtures("no_flush")


def use_ledger(customer_id: str = DAVID, period: str = "2026-10", per_month: int = 1, **kw):
    store.transact_write([benefits.ledger_update(customer_id, period, per_month, **kw)])


# ------------------------------------------------------------------------------ tier


def test_tier_lookup_reads_the_customer_record_and_policy_json(seeded):
    david = benefits.tier_info(DAVID)
    assert (david["tier"], david["tier_label"], david["authority_limit_cents"]) == ("platinum", "Platinum member", 75000)
    assert david["benefits"] == {"free_return_shipping": {"per_month": 1, "applies_to": ["return_mail"], "waives_fee": True}}
    john = benefits.tier_info(JOHN)
    assert (john["tier"], john["tier_label"], john["authority_limit_cents"], john["benefits"]) == (
        "silver", "Silver member", 25000, {})
    ava = benefits.tier_info(AVA)  # the base personas carry tier "none"
    assert (ava["tier"], ava["tier_label"], ava["authority_limit_cents"], ava["benefits"]) == ("none", "Member", 25000, {})
    nobody = benefits.tier_info("DEMO-CUST-NOBODY")  # no record: the plain member
    assert (nobody["tier"], nobody["authority_limit_cents"]) == ("none", 25000)


def test_returned_tier_info_is_a_copy(seeded):
    info = benefits.tier_info(DAVID)
    info["benefits"]["free_return_shipping"]["per_month"] = 99
    assert benefits.tier_info(DAVID)["benefits"]["free_return_shipping"]["per_month"] == 1


# ----------------------------------------------------------------------------- period


@pytest.mark.parametrize(
    ("now", "period"),
    [
        (None, "2026-10"),  # DEMO_NOW
        ("2026-10-31", "2026-10"),
        ("2026-11-01", "2026-11"),
        ("2026-11-01T03:00:00+00:00", "2026-10"),  # 23:00 on Oct 31 in Toronto: still October
        ("2026-11-01T05:00:00+00:00", "2026-11"),  # 01:00 EDT on Nov 1... the clocks go back at 02:00, still November
        ("2026-12-31T23:30:00-05:00", "2026-12"),
        ("2027-01-01T00:30:00-05:00", "2027-01"),
    ],
)
def test_period_is_the_toronto_calendar_month(monkeypatch, now, period):
    monkeypatch.delenv("DEMO_NOW", raising=False)
    assert benefits.period_for(now) == period


def test_period_follows_the_demo_clock_not_the_wall_clock(monkeypatch):
    monkeypatch.setenv("DEMO_NOW", NEXT_MONTH_NOW)
    assert benefits.period_for() == "2026-11"
    monkeypatch.setenv("DEMO_NOW", "2026-10-16T12:00:00-04:00")
    assert benefits.period_for() == "2026-10"


# ------------------------------------------------------------------------------ ledger


def test_availability_follows_the_tier_and_the_ledger(seeded):
    assert benefits.free_return_shipping_available(DAVID) is True
    assert benefits.free_return_shipping_available(JOHN) is False  # silver has no shipping perk
    assert benefits.free_return_shipping_available(AVA) is False
    assert benefits.ledger_usage(DAVID) == {
        "benefit": "free_return_shipping", "tier": "platinum", "period": "2026-10", "used": 0, "allowed": 1}
    assert benefits.ledger_usage(JOHN)["allowed"] == 0

    use_ledger()
    assert ledger()["used"] == 1
    assert benefits.free_return_shipping_available(DAVID) is False
    assert benefits.ledger_usage(DAVID)["used"] == 1
    assert benefits.free_return_shipping_available(DAVID, "2026-11-03") is True  # a new month starts at zero
    assert benefits.ledger_usage(DAVID, "2026-11-03") == {
        "benefit": "free_return_shipping", "tier": "platinum", "period": "2026-11", "used": 0, "allowed": 1}


def test_the_ledger_item_has_the_documented_key_and_small_shape(seeded):
    use_ledger(transaction_id="DEMO-RET-3F9A1C")
    item = store.get_item(("BENEFIT#DEMO-CUST-DAVID#2026-10", "META"))
    assert store.benefit_key(DAVID, "2026-10") == ("BENEFIT#DEMO-CUST-DAVID#2026-10", "META")
    assert item["used"] == 1 and item["period"] == "2026-10" and item["benefit"] == "free_return_shipping"
    assert item["customer_id"] == DAVID and item["last_transaction_id"] == "DEMO-RET-3F9A1C"


def test_the_ledger_update_refuses_a_second_use_and_allows_per_month_uses(seeded):
    use_ledger()
    with pytest.raises(store.TransactionCanceled) as exc:
        use_ledger()  # the condition `used < per_month` fails
    assert exc.value.failed() == ["benefit"]
    assert ledger()["used"] == 1  # never 2

    use_ledger(period="2026-11")  # another month is its own item
    assert ledger(period="2026-11")["used"] == 1 and ledger()["used"] == 1

    use_ledger(period="2026-12", per_month=2)
    use_ledger(period="2026-12", per_month=2)
    assert ledger(period="2026-12")["used"] == 2
    with pytest.raises(store.TransactionCanceled):
        use_ledger(period="2026-12", per_month=2)
    assert ledger(period="2026-12")["used"] == 2


def test_the_benefit_note_is_exact_and_uses_the_count_after_this_use(seeded):
    assert benefits.benefit_note(DAVID, 595) == PERK_OCTOBER
    assert benefits.benefit_note(DAVID, 595, "2026-11-03") == {**PERK_OCTOBER, "period": "2026-11"}
    assert benefits.benefit_note(JOHN, 595) is None and benefits.benefit_note(AVA, 595) is None
    assert benefits.benefit_note(DAVID, 0) is None  # nothing to save
    assert benefits.benefit_note(DAVID, True) is None
    use_ledger()
    assert benefits.benefit_note(DAVID, 595) is None  # used up this month


@pytest.mark.parametrize(
    "perk",
    [
        {"per_month": 0, "applies_to": ["return_mail"], "waives_fee": True},
        {"per_month": True, "applies_to": ["return_mail"], "waives_fee": True},
        {"per_month": 1, "applies_to": ["return_in_store"], "waives_fee": True},
        {"per_month": 1, "applies_to": "return_mail", "waives_fee": True},
        {"per_month": 1, "applies_to": ["return_mail"], "waives_fee": False},
        {"per_month": 1, "applies_to": ["return_mail"]},
        "free",
    ],
)
def test_a_malformed_perk_in_policy_json_is_no_perk(seeded, monkeypatch, perk):
    tiers = policy.load_tiers()
    tiers["platinum"]["benefits"] = {"free_return_shipping": perk}
    monkeypatch.setattr(policy, "load_tiers", lambda: tiers)
    assert benefits.free_return_shipping_config(DAVID) is None
    assert benefits.free_return_shipping_available(DAVID) is False
    assert benefits.benefit_note(DAVID, 595) is None


def test_two_per_month_is_read_from_policy_json(seeded, monkeypatch):
    tiers = policy.load_tiers()
    tiers["platinum"]["benefits"]["free_return_shipping"]["per_month"] = 2
    monkeypatch.setattr(policy, "load_tiers", lambda: tiers)
    use_ledger(per_month=2)
    assert benefits.free_return_shipping_available(DAVID) is True
    assert benefits.benefit_note(DAVID, 595)["used"] == 2 and benefits.benefit_note(DAVID, 595)["allowed"] == 2
    use_ledger(per_month=2)
    assert benefits.free_return_shipping_available(DAVID) is False


# ------------------------------------------------------------------------------- reset


def test_a_demo_reset_clears_the_ledger(seeded):
    use_ledger()
    use_ledger(period="2026-11")
    assert len(keys("BENEFIT#")) == 2 and not benefits.free_return_shipping_available(DAVID)
    assert "BENEFIT#" in store.RUNTIME_PREFIXES

    fixtures.reset("default")
    assert keys("BENEFIT#") == []
    assert benefits.free_return_shipping_available(DAVID) is True  # the perk is back for the next demo run
    assert ledger() is None and ledger(period="2026-11") is None


def test_a_reset_after_a_real_bundle_gives_the_perk_back(flow: Flow):
    from returns_domain import execution

    bundles = Bundles(flow)
    hq = bundles.approved()
    execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    assert ledger()["used"] == 1 and keys("TXN#")
    fixtures.reset("default")
    assert keys("BENEFIT#") == [] and keys("TXN#") == []
    again = bundles.quote()  # a fresh demo run: the waiver is offered again
    assert again.option("return_mail").fee_cents == 0 and again.option("return_mail").benefit["used"] == 1


def test_the_key_builder_rejects_empty_parts():
    with pytest.raises(DomainError):
        store.benefit_key("", "2026-10")
    with pytest.raises(DomainError):
        store.benefit_key(DAVID, "")
