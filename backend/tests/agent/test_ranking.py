"""Ranking rule (PIVOT_ADDENDUM C9): store pickup, then other exchanges, then returns."""

from __future__ import annotations

from agent import ranking

from .fakes import hero_options


def options(**kwargs: object) -> list[dict]:
    return [o.model_dump() for o in hero_options(**kwargs)]  # type: ignore[arg-type]


def ids(ordered: list[dict]) -> list[str]:
    return [o["option_id"] for o in ordered]


def test_pickup_first_then_other_exchanges_then_returns() -> None:
    assert ids(ranking.rank_options(options())) == ["OPT-3", "OPT-2", "OPT-1", "OPT-4", "OPT-5"]


def test_no_deadline_orders_by_eta_inside_a_group() -> None:
    ordered = ranking.rank_options(options(deadline=None))
    assert ids(ordered) == ["OPT-3", "OPT-2", "OPT-1", "OPT-4", "OPT-5"]


def test_options_meeting_the_deadline_lead_their_group() -> None:
    opts = options()
    by_id = {o["option_id"]: o for o in opts}
    by_id["OPT-1"]["eta_date"], by_id["OPT-1"]["meets_deadline"] = "2026-10-17", True  # DC now meets it
    by_id["OPT-2"]["meets_deadline"] = False
    assert ids(ranking.rank_options(opts)) == ["OPT-3", "OPT-1", "OPT-2", "OPT-4", "OPT-5"]


def test_in_store_return_comes_before_mail_in_return() -> None:
    opts = [o for o in options() if o["option_id"] in {"OPT-5", "OPT-4"}]
    assert ids(ranking.rank_options(opts)) == ["OPT-4", "OPT-5"]  # fee 0 before fee 595
    assert ids(ranking.rank_options(list(reversed(opts)))) == ["OPT-4", "OPT-5"]


def test_refund_options_are_never_dropped() -> None:
    ordered = ranking.rank_options(options())
    assert {o["type"] for o in ordered} >= {"return_in_store", "return_mail"}
    assert len(ordered) == 5


def test_unknown_option_types_go_last_and_ties_keep_input_order() -> None:
    opts = [{"option_id": "X", "type": "mystery"}, {"option_id": "A", "type": "return_mail", "fee_cents": 595},
            {"option_id": "B", "type": "exchange_store_pickup", "eta_date": "2026-10-17"}]
    assert ids(ranking.rank_options(opts)) == ["B", "A", "X"]
    twins = [{"option_id": n, "type": "exchange_store_pickup", "eta_date": "2026-10-17", "fee_cents": 0} for n in "CBA"]
    assert ids(ranking.rank_options(twins)) == ["C", "B", "A"]


def test_reason_for_the_hero_ranking() -> None:
    ordered = ranking.rank_options(options())
    reason = ranking.ranking_reason(ordered, "2026-10-17")
    assert reason == (
        "Store pickup at ALDO Sainte-Catherine is free and ready Sat Oct 17, "
        "which meets your Sat Oct 17 deadline. Refund options stay available."
    )
    assert len(reason) <= 240


def test_reason_says_when_the_leading_option_misses_the_deadline() -> None:
    opts = [o for o in options() if o["option_id"] in {"OPT-1", "OPT-4"}]
    reason = ranking.ranking_reason(ranking.rank_options(opts), "2026-10-17")
    assert "does not meet your Sat Oct 17 deadline" in reason and "arrives Tue Oct 20" in reason


def test_reason_without_a_deadline_makes_no_deadline_claim() -> None:
    reason = ranking.ranking_reason(ranking.rank_options(options(deadline=None)), None)
    assert "deadline" not in reason and reason.startswith("Store pickup at ALDO Sainte-Catherine is free")


def test_reason_when_only_returns_exist() -> None:
    opts = [o for o in options() if o["type"].startswith("return")]
    assert "refund options are listed" in ranking.ranking_reason(ranking.rank_options(opts))
    assert ranking.ranking_reason([]) == ""


def test_summary_has_order_reason_and_rule() -> None:
    summary = ranking.ranking_summary(options(), "2026-10-17")
    assert summary["order"] == ["OPT-3", "OPT-2", "OPT-1", "OPT-4", "OPT-5"]
    assert summary["reason"].startswith("Store pickup")
    assert "refund options stay visible" in summary["rule"]


def test_the_owner_chosen_path_goes_first_and_nothing_is_dropped() -> None:
    opts = options()
    assert ids(ranking.rank_options(opts, "store_pickup")) == ["OPT-3", "OPT-2", "OPT-1", "OPT-4", "OPT-5"]  # default
    assert ids(ranking.rank_options(opts, None)) == ids(ranking.rank_options(opts))
    by_mail = ranking.rank_options(opts, "ship")
    assert ids(by_mail) == ["OPT-2", "OPT-1", "OPT-3", "OPT-4", "OPT-5"]  # shipped exchanges, then pickup, then refunds
    refund_first = ranking.rank_options(opts, "return")
    assert ids(refund_first) == ["OPT-4", "OPT-5", "OPT-3", "OPT-2", "OPT-1"]  # in-store before mail-in
    for channel in (None, "store_pickup", "ship", "return"):
        ordered = ranking.rank_options(opts, channel)
        assert sorted(ids(ordered)) == ["OPT-1", "OPT-2", "OPT-3", "OPT-4", "OPT-5"]  # R6.5: all options stay
        assert {"return_in_store", "return_mail"} <= {o["type"] for o in ordered}


def test_an_unknown_channel_means_the_default_order() -> None:
    assert ranking.normalize_channel("teleport") is None and ranking.normalize_channel(None) is None
    assert ids(ranking.rank_options(options(), "teleport")) == ids(ranking.rank_options(options()))


def test_nearest_store_breaks_ties_and_unknown_distance_goes_last() -> None:
    stores = [
        {"option_id": "FAR", "type": "exchange_store_pickup", "eta_date": "2026-10-17", "fee_cents": 0, "minutes_away": 24},
        {"option_id": "NOWHERE", "type": "exchange_store_pickup", "eta_date": "2026-10-17", "fee_cents": 0},
        {"option_id": "NEAR", "type": "exchange_store_pickup", "eta_date": "2026-10-17", "fee_cents": 0, "minutes_away": 5},
    ]
    assert ids(ranking.rank_options(stores)) == ["NEAR", "FAR", "NOWHERE"]


def test_reason_for_a_colour_pickup_names_the_colour_and_how_far() -> None:
    opts = [
        {"option_id": "OPT-2", "type": "exchange_store_pickup", "location_name": "ALDO Sainte-Catherine",
         "eta_date": "2026-10-17", "eta_text": "Sat Oct 17", "fee_cents": 0, "replacement_color": "BROWN",
         "minutes_away": 5, "meets_deadline": None},
        {"option_id": "OPT-4", "type": "return_in_store"},
    ]
    reason = ranking.ranking_reason(ranking.rank_options(opts))
    assert reason == (
        "Store pickup of the Brown pair at ALDO Sainte-Catherine is free and ready Sat Oct 17, "
        "5 minutes away. Refund options stay available."
    )
    one = [{**opts[0], "minutes_away": 1}]
    assert "1 minute away" in ranking.ranking_reason(one) and "1 minutes" not in ranking.ranking_reason(one)


def test_reason_for_the_other_chosen_paths() -> None:
    opts = options()
    by_mail = ranking.ranking_reason(ranking.rank_options(opts, "ship"), None, "ship")
    assert by_mail.startswith("Ship-from-store exchange from ALDO Sainte-Catherine is free and arrives Mon Oct 19")
    assert "as you asked for delivery by mail" in by_mail
    refund = ranking.ranking_reason(ranking.rank_options(opts, "return"), None, "return")
    assert refund == "Refund options lead because you asked for a refund."


def test_a_benefit_gets_a_money_text_and_is_otherwise_left_alone() -> None:
    perk = {"type": "free_return_shipping", "saved_cents": 595, "used": 1, "per_month": 1}
    enriched = ranking.add_money_text({"option_id": "OPT-1", "fee_cents": 0, "benefit": perk})
    assert enriched["benefit"] == {**perk, "saved_text": "CAD 5.95"}
    assert "benefit" not in ranking.add_money_text({"option_id": "OPT-1", "fee_cents": 595})
    assert ranking.add_money_text({"benefit": "not a mapping"})["benefit"] == "not a mapping"


def test_money_text_formats_cents_and_rejects_other_values() -> None:
    assert ranking.money_text(12999) == "CAD 129.99"
    assert ranking.money_text(0) == "CAD 0.00"
    assert ranking.money_text(None) is None and ranking.money_text(12.5) is None and ranking.money_text("5") is None


def test_add_money_text_adds_next_to_cents_fields_only() -> None:
    enriched = ranking.add_money_text({"refund_cents": 12404, "fee_cents": 595, "qty": 1})
    assert enriched["refund_text"] == "CAD 124.04" and enriched["fee_text"] == "CAD 5.95" and "qty_text" not in enriched


def test_reorder_ids_follows_the_known_order() -> None:
    known = ["OPT-3", "OPT-2", "OPT-1", "OPT-4", "OPT-5"]
    assert ranking.reorder_ids(["OPT-5", "OPT-1", "OPT-3"], known) == ["OPT-3", "OPT-1", "OPT-5"]
    assert ranking.reorder_ids(["OPT-9", "OPT-4", "OPT-8"], known) == ["OPT-4", "OPT-9", "OPT-8"]
    assert ranking.reorder_ids(["OPT-4", "OPT-4"], known) == ["OPT-4"]
    assert ranking.reorder_ids(["B", "A"], []) == ["B", "A"]
