"""Pure gateway logic, no AWS: request validation, style resolution, option building, ETAs and
deadline flags, size ordering, templates, the purchase items and the agent card.

Facts come from the real fixture files (``fixtures.build_items`` gives the same items the table
holds), never from hard-coded product names. The behavior that needs a table (proposal,
confirmation, atomicity, replays) is in ``test_gateway_ddb.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from returns_domain import auth, config, fit, fixtures, gateway, options, store
from returns_domain import personal_agent_client
from returns_domain.errors import ErrorCode, DomainError

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 16)  # DEMO_NOW, a Friday
SATURDAY = date(2026, 10, 17)
HERO, CONTRAST, STD6 = "DEMO-STYLE-HERO", "DEMO-STYLE-CONTRAST", "DEMO-STYLE-STD6"
DC = "DEMO-DC-QC"
MTL, LAVAL, TOR = "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01"


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DEMO_NOW",
        "DEMO_WRITEBACK_KEY",
        "SECRETS_ARN",
        "GATEWAY_TRANSPORT",
        "GATEWAY_BASE_URL",
        "DELEGATION_MAX_CENTS",
        "DELEGATION_TTL_SECONDS",
        "RATIONALE_MAX_CHARS",
        "SIZE_REQUEST_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIXTURES_DIR", str(REPO_ROOT / "fixtures"))
    monkeypatch.setenv("DEMO_TOKEN_KEY", "test-only-token-key-0123456789")
    config.clear_secret_cache()
    fit.clear_cache()
    fixtures.clear_cache()

    def no_aws() -> None:
        raise AssertionError("the pure gateway tests must not reach DynamoDB")

    monkeypatch.setattr(store, "get_client", no_aws)


# ----------------------------------------------------------------------------- builders


def claims(
    customer_id: str = "DEMO-CUST-AVA", *, scope: str = "delegated_gateway", cap: int | None = 25_000
) -> auth.TokenClaims:
    now = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
    delegated = scope == "delegated_gateway"
    return auth.TokenClaims(
        customer_id=customer_id,
        scope=scope,
        actor="personal_agent" if delegated else None,
        max_amount_cents=cap if delegated else None,
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def assist_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "intent": "size_assist",
        "product_query": "anything",
        "color": "BLACK",
        "size_system": "US_W",
        "deadline_date": "2026-10-17",
        "channel_pref": None,
    }
    body.update(overrides)
    return body


def decision_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "size_request_id": "SZR-3F9A1C",
        "option_id": "SO-3",
        "chosen_size": "8.0",
        "rationale_public": "runs small per ALDO signal",
        "confidence": "high",
    }
    body.update(overrides)
    return body


def product_name_of(style: str) -> str:
    return next(p["product_name"] for p in fixtures.load_bundle().products if p["style"] == style)


def world(variant: str = "default", style: str = HERO):
    """Products (GSI1 items), INV rows per sku and location items, as the table would hold them."""
    items = fixtures.build_items(fixtures.load_bundle(), variant)
    products = [i for i in items if i["pk"].startswith("PRODUCT#") and i["style"] == style]
    inventory: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["pk"].startswith("INV#"):
            inventory.setdefault(item["sku"], []).append(item)
    locations = {i["location_id"]: i for i in items if i["pk"].startswith("LOC#")}
    return products, inventory, locations


def offered(
    variant: str = "default",
    *,
    style: str = HERO,
    region: str | None = "QC",
    deadline: date | None = SATURDAY,
) -> list[dict[str, Any]]:
    products, inventory, locations = world(variant, style)
    return gateway.build_offered_options(
        products=products,
        inventory=inventory,
        locations=locations,
        home_region=region,
        today=TODAY,
        deadline=deadline,
    )


def by_size(options_: list[dict[str, Any]], size: str) -> list[dict[str, Any]]:
    return [o for o in options_ if o["size"] == size]


def triples(options_: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(o["type"], o["location_id"]) for o in options_]


# ============================================================ scope and request shape (C4, C7)


@pytest.mark.parametrize("scope", ["session", "something_else", None])
def test_gateway_calls_need_the_delegated_scope(scope):
    wrong = claims(scope=scope or "session")
    if scope is None:
        wrong = object()  # no scope attribute at all
    elif scope == "something_else":
        wrong = auth.TokenClaims("DEMO-CUST-AVA", scope, None, None, TODAY, TODAY)  # type: ignore[arg-type]
    for call, body in ((gateway.size_assist, assist_body()), (gateway.decide_size, decision_body())):
        with pytest.raises(DomainError) as exc:
            call(wrong, body)
        assert exc.value.code == ErrorCode.NOT_AUTHORIZED


def test_an_empty_customer_in_the_claims_is_refused():
    with pytest.raises(DomainError) as exc:
        gateway.size_assist(claims(customer_id=""), assist_body())
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED


@pytest.mark.parametrize(
    "extra",
    [
        {"customer_id": "DEMO-CUST-BEN"},
        {"owner_ref": "DEMO-CUST-AVA"},
        {"owner_fit_hint": {"typical_size": "7.5"}},
        {"typical_size": "7.5"},
        {"home_region": "ON"},
        {"retailer": "Retailer A (synthetic)"},
        {"unknown": 1},
    ],
)
def test_extra_fields_are_forbidden(extra):
    for call, body in ((gateway.size_assist, assist_body(**extra)), (gateway.decide_size, decision_body(**extra))):
        with pytest.raises(DomainError) as exc:
            call(claims(), body)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert "not permitted" in exc.value.message
        for secret in extra.values():
            assert str(secret) not in exc.value.message  # the offending value is never echoed


@pytest.mark.parametrize("body", [None, [], "size_assist", 3, ("a",)])
def test_the_body_must_be_a_json_object(body):
    for call in (gateway.size_assist, gateway.decide_size):
        with pytest.raises(DomainError) as exc:
            call(claims(), body)  # type: ignore[arg-type]
        assert exc.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize(
    "overrides",
    [
        {"intent": "buy"},
        {"intent": ""},
        {"product_query": "a"},
        {"product_query": "x" * 81},
        {"product_query": 12},
        {"color": "x" * 21},
        {"color": 5},
        {"size_system": "EU"},
        {"size_system": "us_w"},
        {"channel_pref": "return"},
        {"channel_pref": "SHIP"},
        {"deadline_date": "10/17/2026"},
        {"deadline_date": "2026-02-30"},
        {"deadline_date": "2026-10-17T12:00:00"},
        {"deadline_date": 20261017},
    ],
)
def test_size_assist_body_is_checked(overrides):
    with pytest.raises(DomainError) as exc:
        gateway.size_assist(claims(), assist_body(**overrides))
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_intent_is_required():
    body = assist_body()
    del body["intent"]
    with pytest.raises(DomainError) as exc:
        gateway.size_assist(claims(), body)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_size_assist_accepts_the_documented_forms():
    for overrides in (
        {},
        {"color": None},
        {"color": ""},
        {"color": "black"},
        {"deadline_date": None},
        {"deadline_date": ""},
        {"channel_pref": "ship"},
        {"channel_pref": "store_pickup"},
        {"product_query": "  padded  "},
        {"size_system": "US_W"},
    ):
        gateway.SizeAssistRequest.model_validate(assist_body(**overrides))
    parsed = gateway.SizeAssistRequest.model_validate(assist_body(color="", deadline_date="", product_query="  Ab  "))
    assert (parsed.color, parsed.deadline_date, parsed.product_query) == (None, None, "Ab")
    assert gateway.SizeAssistRequest.model_validate({"intent": "size_assist", "product_query": "ab"}).size_system == "US_W"


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_request_id": "SZR-3f9a1c"},
        {"size_request_id": "SZR-3F9A1"},
        {"size_request_id": "szr-3F9A1C"},
        {"size_request_id": "SZR-3F9A1C; DROP"},
        {"size_request_id": 5},
        {"option_id": "SO-"},
        {"option_id": "SO-1000"},
        {"option_id": "so-3"},
        {"option_id": "OPT-3"},
        {"chosen_size": "7.25"},
        {"chosen_size": "eight"},
        {"chosen_size": True},
        {"chosen_size": None},
        {"chosen_size": "0"},
        {"chosen_size": "31"},
        {"chosen_size": ["8.0"]},
        {"rationale_public": "x" * 121},
        {"rationale_public": 5},
        {"confidence": "med"},
        {"confidence": "HIGH"},
        {"confidence": 1},
    ],
)
def test_size_decision_body_is_checked(overrides):
    with pytest.raises(DomainError) as exc:
        gateway.decide_size(claims(), decision_body(**overrides))
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_size_decision_accepts_the_documented_forms():
    for overrides in (
        {},
        {"chosen_size": 8},
        {"chosen_size": 8.0},
        {"chosen_size": "8"},
        {"chosen_size": "8.50"},
        {"chosen_size": " 7.5 "},
        {"rationale_public": ""},
        {"rationale_public": "x" * 120},
        {"confidence": "low"},
        {"confidence": "medium"},
        {"option_id": "SO-999"},
    ):
        gateway.SizeDecisionRequest.model_validate(decision_body(**overrides))
    body = {k: v for k, v in decision_body().items() if k not in ("rationale_public", "confidence")}
    parsed = gateway.SizeDecisionRequest.model_validate(body)
    assert (parsed.rationale_public, parsed.confidence) == ("", "medium")


def test_chosen_size_is_normalized_through_store_size_str():
    for raw, expected in ((8, "8.0"), (8.0, "8.0"), ("8", "8.0"), ("8.50", "8.5"), (" 7.5 ", "7.5")):
        parsed = gateway.SizeDecisionRequest.model_validate(decision_body(chosen_size=raw))
        assert parsed.chosen_size == expected == store.size_str(raw)


def test_rationale_is_data_control_characters_and_whitespace_are_flattened():
    messy = "runs small\n\tper ALDO\x00 signal   \u2028 ok\r\n"
    parsed = gateway.SizeDecisionRequest.model_validate(decision_body(rationale_public=messy))
    assert parsed.rationale_public == "runs small per ALDO signal ok"


def test_rationale_limit_follows_the_setting(monkeypatch):
    monkeypatch.setenv("RATIONALE_MAX_CHARS", "10")
    with pytest.raises(DomainError):
        gateway.decide_size(claims(), decision_body(rationale_public="x" * 11))
    gateway.SizeDecisionRequest.model_validate(decision_body(rationale_public="x" * 10))


def test_validation_messages_name_the_field_not_the_value():
    secret = "Bearer eyJhbGciOiJIUzI1NiJ9.leaked.token"
    with pytest.raises(DomainError) as exc:
        gateway.decide_size(claims(), decision_body(option_id=secret))
    assert "option_id" in exc.value.message and secret not in exc.value.message
    with pytest.raises(DomainError) as exc:
        gateway.size_assist(claims(), assist_body(deadline_date=secret))
    assert "deadline_date" in exc.value.message and secret not in exc.value.message


# ============================================================ style resolution (F.3 step 3)


def fragment(name: str) -> str:
    """A shortest lowercase prefix of ``name`` that matches no other product name."""
    others = [p["product_name"].casefold() for p in fixtures.load_bundle().products if p["product_name"] != name]
    for length in range(2, len(name) + 1):
        if not any(prefix in other for other in others for prefix in [name[:length].casefold()]):
            return name[:length].lower()
    return name.lower()


def test_a_substring_in_any_case_finds_the_style():
    for style, color in ((HERO, "BLACK"), (CONTRAST, "BLACK"), (STD6, "GREY")):
        name = product_name_of(style)
        for query in (name, name.lower(), name.upper(), fragment(name), f"  {fragment(name)}  "):
            assert gateway.resolve_style(query) == (name, style, color)


def test_an_exact_name_wins_even_when_it_is_also_part_of_longer_names(monkeypatch):
    catalog = {
        "Lagoon": {"DEMO-STYLE-A": ["BLACK"]},
        "Lagoon Lite": {"DEMO-STYLE-B": ["TAN"]},
    }
    monkeypatch.setattr(gateway, "_catalog", lambda: catalog)
    assert gateway.resolve_style("lagoon") == ("Lagoon", "DEMO-STYLE-A", "BLACK")
    assert gateway.resolve_style("lagoon l")[0] == "Lagoon Lite"


def test_an_unknown_product_lists_no_candidates():
    with pytest.raises(gateway.GatewayError) as exc:
        gateway.resolve_style("zzzzzz")
    error = exc.value
    assert error.code == ErrorCode.UNKNOWN_STYLE and error.http_status == 422 and not error.retryable
    assert error.to_dict()["candidates"] == []


def test_an_ambiguous_query_lists_every_matching_style_with_its_colors():
    query = "e"  # matches many names
    with pytest.raises(gateway.GatewayError) as exc:
        gateway.resolve_style(query)
    candidates = exc.value.to_dict()["candidates"]
    assert 1 < len(candidates) <= 10
    assert all(set(c) == {"style_id", "product_name", "colors"} for c in candidates)
    assert all(query in c["product_name"].casefold() for c in candidates)
    assert all(isinstance(c["colors"], list) and c["colors"] for c in candidates)
    hero = next((c for c in candidates if c["style_id"] == HERO), None)
    if hero:
        assert hero["colors"] == ["BLACK"] and hero["product_name"] == product_name_of(HERO)


def test_a_query_longer_than_any_name_is_no_match_not_an_error():
    with pytest.raises(gateway.GatewayError) as exc:
        gateway.resolve_style("a" * 70)
    assert exc.value.to_dict()["candidates"] == []


def test_the_color_is_matched_case_insensitively_and_unknown_colors_name_the_style():
    name = product_name_of(HERO)
    assert gateway.resolve_style(name, "black")[2] == "BLACK"
    assert gateway.resolve_style(name, "Black")[2] == "BLACK"
    with pytest.raises(gateway.GatewayError) as exc:
        gateway.resolve_style(name, "NEON")
    assert exc.value.code == ErrorCode.UNKNOWN_STYLE
    assert exc.value.to_dict()["candidates"] == [{"style_id": HERO, "product_name": name, "colors": ["BLACK"]}]


def test_a_style_with_several_colors_needs_a_color(monkeypatch):
    monkeypatch.setattr(gateway, "_catalog", lambda: {"Duo": {"DEMO-STYLE-D": ["BLACK", "TAN"]}})
    with pytest.raises(gateway.GatewayError) as exc:
        gateway.resolve_style("duo")
    assert exc.value.to_dict()["candidates"][0]["colors"] == ["BLACK", "TAN"]
    assert gateway.resolve_style("duo", "tan")[2] == "TAN"


def test_a_name_shared_by_two_styles_is_ambiguous(monkeypatch):
    monkeypatch.setattr(
        gateway, "_catalog", lambda: {"Twin": {"DEMO-STYLE-A": ["BLACK"], "DEMO-STYLE-B": ["BLACK"]}}
    )
    with pytest.raises(gateway.GatewayError) as exc:
        gateway.resolve_style("twin")
    assert {c["style_id"] for c in exc.value.to_dict()["candidates"]} == {"DEMO-STYLE-A", "DEMO-STYLE-B"}


def test_the_query_is_data_never_a_pattern():
    for query in (".*", "[a-z]+", "Stessy|Krios", "%", "x' OR '1'='1", "\x00"):
        with pytest.raises(gateway.GatewayError) as exc:
            gateway.resolve_style(query)
        assert exc.value.to_dict()["candidates"] == []


def test_size_assist_reports_unknown_style_before_touching_stock():
    with pytest.raises(gateway.GatewayError) as exc:  # the autouse guard fails the test on any AWS call
        gateway.size_assist(claims(), assist_body(product_query="no such shoe"))
    assert exc.value.http_status == 422


def test_gateway_error_carries_extras_beside_the_code():
    error = gateway.GatewayError("UNKNOWN_STYLE", "Which?", extra={"candidates": [{"style_id": "S"}]})
    assert isinstance(error, DomainError)
    assert error.to_dict() == {
        "code": "UNKNOWN_STYLE",
        "message": "Which?",
        "retryable": False,
        "candidates": [{"style_id": "S"}],
    }
    assert error.details == {"candidates": [{"style_id": "S"}]}
    assert str(error) == "UNKNOWN_STYLE: Which?"
    assert gateway.GatewayError("VALIDATION_ERROR", "x").to_dict() == {
        "code": "VALIDATION_ERROR",
        "message": "x",
        "retryable": False,
    }


# ====================================================== option building (F.3 step 5, C5)


def test_the_hero_run_is_offered_in_numeric_size_order_with_global_option_ids():
    result = offered()
    assert [o["size"] for o in result if o["type"] == "ship_from_dc"] == ["6.0", "6.5", "7.0", "7.5", "8.0", "8.5", "9.0"]
    assert [o["option_id"] for o in result] == [f"SO-{n}" for n in range(1, len(result) + 1)]
    sizes_in_order = [o["size"] for o in result]
    assert sizes_in_order == sorted(sizes_in_order, key=float)


def test_sizes_sort_by_value_not_by_text():
    products = [{"sku": f"S{s}", "size": s} for s in ("9.0", "10.0", "8.5", "6.0", "10.5", "11.0")]
    inventory = {p["sku"]: [{"sk": f"LOC#{DC}", "location_id": DC, "atp": 1, "version": 1}] for p in products}
    locations = {DC: world()[2][DC]}
    result = gateway.build_offered_options(
        products=products, inventory=inventory, locations=locations, home_region="QC", today=TODAY, deadline=None
    )
    assert [o["size"] for o in result] == ["6.0", "8.5", "9.0", "10.0", "10.5", "11.0"]
    assert [c.size for c in gateway.group_sizes(result)] == ["6.0", "8.5", "9.0", "10.0", "10.5", "11.0"]


def test_within_a_size_the_order_is_dc_then_store_ship_then_pickups_by_location_id():
    size_75 = by_size(offered(), "7.5")
    assert triples(size_75) == [("ship_from_dc", DC), ("ship_from_store", MTL), ("store_pickup", MTL)]
    size_60 = by_size(offered(), "6.0")  # LAVAL and MTL both hold one: lowest id first
    assert triples(size_60) == [
        ("ship_from_dc", DC),
        ("ship_from_store", LAVAL),
        ("store_pickup", LAVAL),
        ("store_pickup", MTL),
    ]


def test_etas_count_calendar_days_from_the_demo_date():
    size_75 = {o["type"]: o for o in by_size(offered(), "7.5")}
    assert size_75["ship_from_dc"]["eta_date"] == "2026-10-20"  # Tuesday, DC ship_days 4
    assert size_75["ship_from_store"]["eta_date"] == "2026-10-19"  # Monday, store ship_days 3
    assert size_75["store_pickup"]["eta_date"] == "2026-10-17"  # Saturday, pickup_ready_days 1
    assert TODAY.strftime("%A") == "Friday" and SATURDAY.strftime("%A") == "Saturday"


def test_only_store_pickup_meets_saturday():
    for option in offered(deadline=SATURDAY):
        assert option["meets_deadline"] is (option["type"] == "store_pickup")


def test_deadline_flags_follow_the_eta_exactly():
    def flags(deadline: date | None) -> dict[str, bool | None]:
        return {o["type"]: o["meets_deadline"] for o in by_size(offered(deadline=deadline), "7.5")}

    assert flags(None) == {"ship_from_dc": None, "ship_from_store": None, "store_pickup": None}
    assert flags(TODAY) == {"ship_from_dc": False, "ship_from_store": False, "store_pickup": False}
    assert flags(SATURDAY - timedelta(days=0)) == {"ship_from_dc": False, "ship_from_store": False, "store_pickup": True}
    assert flags(date(2026, 10, 19)) == {"ship_from_dc": False, "ship_from_store": True, "store_pickup": True}
    assert flags(date(2026, 10, 20)) == {"ship_from_dc": True, "ship_from_store": True, "store_pickup": True}
    assert flags(date(2026, 12, 31)) == {"ship_from_dc": True, "ship_from_store": True, "store_pickup": True}


def test_atp_and_inventory_versions_come_from_the_rows():
    size_75 = {o["type"]: o for o in by_size(offered(), "7.5")}
    assert (size_75["ship_from_dc"]["atp"], size_75["ship_from_store"]["atp"], size_75["store_pickup"]["atp"]) == (5, 1, 1)
    assert all(o["inventory_version"] == 1 for o in offered())
    products, inventory, locations = world()
    for rows in inventory.values():
        for row in rows:
            if row["location_id"] == DC:
                row["version"] = 7
    result = gateway.build_offered_options(
        products=products, inventory=inventory, locations=locations, home_region="QC", today=TODAY, deadline=None
    )
    assert {o["inventory_version"] for o in result if o["type"] == "ship_from_dc"} == {7}
    assert {o["inventory_version"] for o in result if o["type"] != "ship_from_dc"} == {1}


def test_store_options_exist_only_for_the_owners_home_region():
    ava = offered(region="QC")
    assert {o["location_id"] for o in ava if o["type"] != "ship_from_dc"} <= {MTL, LAVAL}
    ben = offered(region="ON")
    assert {o["location_id"] for o in ben if o["type"] != "ship_from_dc"} == {TOR}
    assert triples(by_size(ben, "7.5")) == [("ship_from_dc", DC), ("ship_from_store", TOR), ("store_pickup", TOR)]
    assert triples(by_size(ben, "6.0")) == [("ship_from_dc", DC)]  # Toronto has none of that size
    nowhere = offered(region=None)
    assert {o["type"] for o in nowhere} == {"ship_from_dc"}
    assert {o["type"] for o in offered(region="BC")} == {"ship_from_dc"}


def test_store_ship_is_the_single_cheapest_store_ties_broken_by_location_id():
    products, inventory, locations = world()
    locations = {k: dict(v) for k, v in locations.items()}
    locations[LAVAL]["ship_days"] = 2  # Laval is now faster than Montreal
    result = gateway.build_offered_options(
        products=products, inventory=inventory, locations=locations, home_region="QC", today=TODAY, deadline=None
    )
    size_60 = [o for o in result if o["size"] == "6.0" and o["type"] == "ship_from_store"]
    assert [(o["location_id"], o["eta_date"]) for o in size_60] == [(LAVAL, "2026-10-18")]
    locations[LAVAL]["ship_days"] = 5  # now slower
    result = gateway.build_offered_options(
        products=products, inventory=inventory, locations=locations, home_region="QC", today=TODAY, deadline=None
    )
    assert [o["location_id"] for o in result if o["size"] == "6.0" and o["type"] == "ship_from_store"] == [MTL]
    assert len([o for o in result if o["size"] == "6.0" and o["type"] == "ship_from_store"]) == 1


def test_dc_stockout_leaves_only_montreal_and_only_pickup_meets_saturday():
    result = offered("dc_stockout")
    for size in ("7.5", "8.0"):
        assert triples(by_size(result, size)) == [("ship_from_store", MTL), ("store_pickup", MTL)]
        assert [o["atp"] for o in by_size(result, size)] == [2, 2]
        meets = [o for o in by_size(result, size) if o["meets_deadline"]]
        assert [(o["type"], o["location_id"]) for o in meets] == [("store_pickup", MTL)]
    assert triples(by_size(result, "6.0")) == triples(by_size(offered(), "6.0"))  # other sizes untouched


def test_all_stockout_drops_the_two_sold_out_sizes_entirely():
    result = offered("all_stockout")
    sizes = {o["size"] for o in result}
    assert "7.5" not in sizes and "8.0" not in sizes
    assert sizes == {"6.0", "6.5", "7.0", "8.5", "9.0"}
    assert [c.size for c in gateway.group_sizes(result)] == ["6.0", "6.5", "7.0", "8.5", "9.0"]


def test_rows_without_stock_or_without_a_location_are_ignored():
    products, inventory, locations = world()
    sku = next(p["sku"] for p in products if p["size"] == "7.5")
    inventory[sku].append({"sk": "LOC#DEMO-GHOST", "location_id": "DEMO-GHOST", "atp": 9, "version": 1})
    for row in inventory[sku]:
        if row["location_id"] == DC:
            row["atp"] = 0
    result = gateway.build_offered_options(
        products=products, inventory=inventory, locations=locations, home_region="QC", today=TODAY, deadline=None
    )
    assert "ship_from_dc" not in {o["type"] for o in result if o["size"] == "7.5"}
    assert "DEMO-GHOST" not in {o["location_id"] for o in result}
    for row in inventory[sku]:
        row["atp"] = 0
    result = gateway.build_offered_options(
        products=products, inventory=inventory, locations=locations, home_region="QC", today=TODAY, deadline=None
    )
    assert "7.5" not in {o["size"] for o in result}


def test_every_option_names_a_real_sku_and_location():
    products, _, locations = world()
    skus = {p["sku"]: p["size"] for p in products}
    for option in offered():
        assert skus[option["sku"]] == option["size"]
        assert option["location_id"] in locations
        assert option["location_name"] == locations[option["location_id"]]["name"]
        assert option["type"] in gateway.OPTION_TYPES


def test_eta_for_option_agrees_with_the_returns_options_rule():
    _, _, locations = world()
    kinds = {
        "ship_from_dc": "exchange_ship_from_dc",
        "ship_from_store": "exchange_ship_from_store",
        "store_pickup": "exchange_store_pickup",
    }
    for location in locations.values():
        for gateway_type, returns_type in kinds.items():
            if gateway_type == "store_pickup" and location["type"] != "STORE":
                continue
            assert gateway.eta_for_option(location, gateway_type, TODAY) == options.eta_for(location, returns_type, TODAY)
    with pytest.raises(DomainError):
        gateway.eta_for_option({"ship_days": None}, "ship_from_dc", TODAY)


def test_group_sizes_shapes_the_response():
    sizes = gateway.group_sizes(offered())
    assert [c.size for c in sizes] == ["6.0", "6.5", "7.0", "7.5", "8.0", "8.5", "9.0"]
    first = sizes[3].options[2].model_dump()
    assert first == {
        "option_id": "SO-15",
        "type": "store_pickup",
        "location_id": MTL,
        "location_name": "ALDO Demo Store Montreal",
        "eta_date": "2026-10-17",
        "eta_text": "Sat Oct 17",
        "meets_deadline": True,
        "atp": 1,
    }
    assert gateway.group_sizes([]) == []


# ================================================================ question and fit signal


def view(signal: str, share: float | None, n: int, scope: str = "ALDO_CA") -> gateway.FitSignalView:
    return gateway.FitSignalView(signal=signal, too_small_share=share, n_fit=n, scope=scope)


def test_question_templates_use_the_real_numbers():
    ask = "Which size and option should I reserve for your owner?"
    assert gateway.question_text(view("runs_small", 0.799, 582)) == (
        f"This style runs small (80% of fit-related returns were 'too small', n=582). {ask}"
    )
    assert gateway.question_text(view("runs_large", 0.151, 417)) == (
        f"This style runs large (85% of fit-related returns were 'too big', n=417). {ask}"
    )
    assert gateway.question_text(view("mixed", 0.59, 222)) == f"Returns for this style are mixed on fit (n=222). {ask}"
    assert gateway.question_text(view("insufficient_data", None, 12)) == (
        f"ALDO has too little return data on this style for a fit signal (n=12). {ask}"
    )
    assert gateway.question_text(view("insufficient_data", None, 0)).startswith("ALDO has too little return data")


def test_question_text_rounds_like_the_contract():
    for share in (0.7, 0.705, 0.71, 0.6549, 0.8):
        text = gateway.question_text(view("runs_small", share, 100))
        assert f"({round(share * 100)}%" in text
    for share in (0.3, 0.151, 0.2):
        text = gateway.question_text(view("runs_large", share, 100))
        assert f"({round((1 - share) * 100)}%" in text


def test_question_text_accepts_a_plain_mapping():
    plain = {"signal": "mixed", "too_small_share": 0.5, "n_fit": 40, "scope": "ALDO_CA", "provenance": "derived"}
    assert "n=40" in gateway.question_text(plain)


def test_the_question_is_built_from_server_numbers_only():
    text = gateway.question_text(view("runs_small", 0.9, 100))
    assert "ignore" not in text.lower() and len(text) < 200


def test_the_hero_signal_comes_from_the_derived_file():
    insight = fit.get_fit_insight(product_name_of(HERO))
    signal = gateway.fit_signal_view(insight)
    assert signal.model_dump() == {
        "signal": "runs_small",
        "too_small_share": insight["too_small_share"],
        "n_fit": insight["n_fit"],
        "scope": "ALDO_CA",
        "provenance": "derived",
    }
    assert insight["n_fit"] >= 100 and insight["too_small_share"] >= 0.65
    share, n = insight["too_small_share"], insight["n_fit"]
    assert gateway.question_text(signal).startswith(
        f"This style runs small ({round(share * 100)}% of fit-related returns were 'too small', n={n}).")


def test_the_contrast_runs_large_and_a_thin_style_falls_back_to_the_wider_scope():
    contrast = gateway.fit_signal_view(fit.get_fit_insight(product_name_of(CONTRAST)))
    assert contrast.signal == "runs_large" and contrast.scope == "ALDO_CA"
    thin = fit.get_fit_insight(product_name_of(STD6))
    assert thin["scopes"]["ALDO_CA"]["signal"] == "insufficient_data"
    view_ = gateway.fit_signal_view(thin)
    assert view_.scope == "ALL" and view_.signal != "insufficient_data"  # labeled, never silently mixed


# ================================================================== purchase items (F.4 step 9)


def record(**overrides: Any) -> dict[str, Any]:
    option = {
        "size": "8.0",
        "type": "store_pickup",
        "sku": "DEMO-SKU-HERO-BLACK-080",
        "location_id": MTL,
        "location_name": "ALDO Demo Store Montreal",
        "eta_date": "2026-10-17",
        "meets_deadline": True,
        "atp": 1,
        "inventory_version": 1,
    }
    base = {
        "size_request_id": "SZR-3F9A1C",
        "customer_id": "DEMO-CUST-AVA",
        "status": "proposed",
        "style_id": HERO,
        "product_name": product_name_of(HERO),
        "color": "BLACK",
        "price_cents": 12999,
        "currency": "CAD",
        "expires_at": "2026-10-16T16:15:00+00:00",
        "offered": {
            "SO-3": option,
            "SO-4": {**option, "type": "ship_from_dc", "location_id": DC, "location_name": "ALDO Demo Distribution Centre", "eta_date": "2026-10-20"},
        },
    }
    base.update(overrides)
    return base


def test_the_order_is_derived_from_the_request_id():
    plan = gateway.build_purchase(record=record(), option_id="SO-3", ordered_on=TODAY)
    expected = store.derived_id("DEMO-A2A-", "SZR-3F9A1C", n=6)
    assert plan["order_id"] == expected and re.fullmatch(r"DEMO-A2A-[0-9A-F]{6}", expected)
    again = gateway.build_purchase(record=record(), option_id="SO-3", ordered_on=TODAY)
    assert again["order_id"] == plan["order_id"]  # the same request always means the same order
    other = gateway.build_purchase(record=record(size_request_id="SZR-AAAAAA"), option_id="SO-3", ordered_on=TODAY)
    assert other["order_id"] != plan["order_id"]


def test_the_order_and_line_items_match_the_fixture_shape():
    plan = gateway.build_purchase(record=record(), option_id="SO-3", ordered_on=TODAY)
    meta, line = plan["items"]
    oid = plan["order_id"]
    assert (meta["pk"], meta["sk"]) == (f"ORDER#{oid}", "META")
    assert (meta["gsi1pk"], meta["gsi1sk"]) == ("CUST#DEMO-CUST-AVA", f"ORDER#2026-10-16#{oid}")
    assert meta | {"pk": 0, "sk": 0, "gsi1pk": 0, "gsi1sk": 0} == {
        "pk": 0,
        "sk": 0,
        "gsi1pk": 0,
        "gsi1sk": 0,
        "order_id": oid,
        "customer_id": "DEMO-CUST-AVA",
        "banner": "ALDO_CA",
        "currency": "CAD",
        "channel": "agent",
        "ordered_at": "2026-10-16",
        "delivered_at": None,
        "size_request_id": "SZR-3F9A1C",
        "fulfillment": {
            "type": "store_pickup",
            "location_id": MTL,
            "location_name": "ALDO Demo Store Montreal",
            "eta_date": "2026-10-17",
        },
        "source": "synthetic",
    }
    assert (line["pk"], line["sk"]) == (f"ORDER#{oid}", f"LINE#{oid}-L1")
    assert line["order_id"] == oid and line["line_id"] == f"{oid}-L1"
    assert (line["sku"], line["size"], line["color"], line["style"]) == ("DEMO-SKU-HERO-BLACK-080", "8.0", "BLACK", HERO)
    assert (line["purchased_qty"], line["returned_or_reserved_qty"], line["unit_paid_cents"]) == (1, 0, 12999)
    assert line["final_sale"] is False and line["source"] == "synthetic"


def test_the_price_is_the_one_aldo_recorded_not_anything_the_agent_said():
    plan = gateway.build_purchase(record=record(price_cents=28900), option_id="SO-3", ordered_on=TODAY)
    assert plan["items"][1]["unit_paid_cents"] == 28900
    assert plan["result"].line["unit_paid_cents"] == 28900
    with pytest.raises(DomainError):
        gateway.build_purchase(record=record(price_cents=129.99), option_id="SO-3", ordered_on=TODAY)


def test_the_response_has_the_contract_shape_and_no_identifiers():
    plan = gateway.build_purchase(record=record(), option_id="SO-3", ordered_on=TODAY)
    body = plan["result"].model_dump(mode="json")
    assert body == {
        "order_id": plan["order_id"],
        "status": "placed_simulated",
        "line": {"sku": "DEMO-SKU-HERO-BLACK-080", "size": "8.0", "unit_paid_cents": 12999},
        "fulfillment": {
            "type": "store_pickup",
            "location_id": MTL,
            "location_name": "ALDO Demo Store Montreal",
            "eta_date": "2026-10-17",
        },
        "next_step": "Pick up size 8.0 at ALDO Demo Store Montreal from Sat Oct 17. Simulated order, no payment taken.",
        "replayed": False,
    }
    assert "DEMO-CUST" not in json.dumps(body)


def test_next_step_text_for_pickup_and_shipping():
    pickup = record()["offered"]["SO-3"]
    ship = record()["offered"]["SO-4"]
    assert gateway.next_step_text(pickup) == (
        "Pick up size 8.0 at ALDO Demo Store Montreal from Sat Oct 17. Simulated order, no payment taken."
    )
    assert gateway.next_step_text(ship) == (
        "Size 8.0 ships from ALDO Demo Distribution Centre and should arrive by Tue Oct 20. "
        "Simulated order, no payment taken."
    )
    assert gateway.next_step_text({**ship, "type": "ship_from_store", "eta_date": "2026-10-19"}).endswith(
        "should arrive by Mon Oct 19. Simulated order, no payment taken."
    )


def test_the_proposal_summary_is_rendered_from_the_recorded_offer():
    summary = gateway._option_summary(record(), "SO-3")
    assert summary == {
        "product_name": product_name_of(HERO),
        "color": "BLACK",
        "option_id": "SO-3",
        "size": "8.0",
        "option_type": "store_pickup",
        "location_id": MTL,
        "location_name": "ALDO Demo Store Montreal",
        "eta_date": "2026-10-17",
        "eta_text": "Sat Oct 17",
        "meets_deadline": True,
        "price_cents": 12999,
        "currency": "CAD",
    }


# ======================================================================== agent card


def test_the_agent_card_lists_the_three_skills_and_the_delegated_scheme():
    card = gateway.agent_card("https://d111.cloudfront.net/")
    assert card["name"] == "ALDO Fit Gateway"
    assert card["url"] == "https://d111.cloudfront.net/api/gateway"
    assert card["version"] and card["description"]
    assert [s["id"] for s in card["skills"]] == ["size_assist", "size_decision", "memory_writeback"]
    for skill in card["skills"]:
        assert skill["name"] and skill["description"] and isinstance(skill["tags"], list)
    assert card["securitySchemes"]["delegatedBearer"]["type"] == "http"
    assert card["securitySchemes"]["delegatedBearer"]["scheme"] == "bearer"
    assert "delegated_gateway" in card["securitySchemes"]["delegatedBearer"]["description"]
    assert "CAD 250.00" in card["securitySchemes"]["delegatedBearer"]["description"]
    assert card["security"] == [{"delegatedBearer": []}]
    assert card["provider"]["organization"] == "ALDO (prototype, simulated data)"
    assert card["label"] == "Prototype, simulated ALDO data, not official policy"
    assert card["provenance"] == "synthetic demo"
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False}
    assert card["defaultInputModes"] == ["application/json"] == card["defaultOutputModes"]


def test_the_agent_card_calls_the_personal_agent_alado_bot():
    card = gateway.agent_card("https://example.com")
    text = json.dumps(card)
    assert "ALDO bot" in card["description"]
    assert "Muse" not in text
    assert "owner" in card["skills"][1]["description"] and "confirm" in card["skills"][1]["description"]


def test_the_agent_card_is_public_data_only():
    card = gateway.agent_card("http://localhost:3000")
    text = json.dumps(card)
    assert json.loads(text) == card  # plain JSON
    assert "test-only-token-key" not in text and "DEMO-CUST" not in text and "Bearer ey" not in text
    assert card["url"] == "http://localhost:3000/api/gateway"


@pytest.mark.parametrize("bad", ["", "ftp://x", "example.com", None, 5])
def test_the_agent_card_needs_an_http_base_url(bad):
    with pytest.raises(DomainError) as exc:
        gateway.agent_card(bad)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_the_agent_card_follows_the_delegation_settings(monkeypatch):
    monkeypatch.setenv("DELEGATION_MAX_CENTS", "10000")
    description = gateway.agent_card("https://example.com")["securitySchemes"]["delegatedBearer"]["description"]
    assert "CAD 100.00" in description and "10 minutes" in description


# ====================================================================== module boundary


def test_returns_domain_never_imports_the_personal_agent():
    for module in (gateway, personal_agent_client):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert "personal_agent" not in imported, module.__name__


def test_the_public_names_exist():
    for name in gateway.__all__:
        assert hasattr(gateway, name), name
    for name in (
        "size_assist",
        "decide_size",
        "get_proposal",
        "confirm_purchase",
        "simulate_delivery",
        "simulate_pickup",
        "build_memory_update",
        "enqueue_memory_update",
        "flush_outbox",
        "agent_card",
        "GatewayError",
        "PersonalAgentClient",
        "get_client",
    ):
        assert hasattr(gateway, name), name
    assert gateway.get_client is personal_agent_client.get_client


def test_signatures_follow_the_contract():
    def params(fn: Any) -> list[str]:
        return list(inspect.signature(fn).parameters)

    assert params(gateway.size_assist) == ["claims", "request"]
    assert params(gateway.decide_size) == ["claims", "request"]
    assert params(gateway.get_proposal) == ["customer_id", "size_request_id"]
    assert params(gateway.confirm_purchase) == ["customer_id", "size_request_id", "option_id"]
    assert inspect.signature(gateway.confirm_purchase).parameters["option_id"].default is None
    assert params(gateway.simulate_delivery) == ["customer_id", "order_id"]
    assert params(gateway.simulate_pickup) == ["customer_id", "transaction_id"]
    assert params(gateway.flush_outbox) == ["customer_id", "limit", "client"]
    assert inspect.signature(gateway.flush_outbox).parameters["limit"].default == 10
    assert params(gateway.agent_card) == ["base_url"]
    assert params(gateway.build_memory_update) == ["txn", "outcome", "provisional"]
    assert params(gateway.enqueue_memory_update) == ["update"]
