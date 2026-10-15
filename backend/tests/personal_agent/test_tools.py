"""Tool behavior: rationale sanitizing, offered-choice checks, events, memory recall (no model)."""

from __future__ import annotations

from typing import Any

import pytest

from personal_agent import memory, tools
from personal_agent.memory import MemoryRepo
from personal_agent.tools import PersonalRunContext

from .canned import FakeGateway, proposed_response

AVA = "DEMO-CUST-AVA"


def make_ctx(repo: MemoryRepo, gateway: FakeGateway | None = None, owner: str = AVA) -> PersonalRunContext:
    conv = repo.create_conversation(owner)
    run_id = memory.new_run_id()
    repo.create_run(owner, conv, run_id)
    repo.mark_started(owner, run_id)
    return PersonalRunContext(
        run_id=run_id, conv_id=conv, owner_id=owner, gateway=gateway or FakeGateway(), repo=repo
    )


def events(ctx: PersonalRunContext, kind: str | None = None) -> list[dict[str, Any]]:
    page = ctx.repo.list_events(ctx.owner_id, ctx.run_id)["events"]
    return [e for e in page if kind is None or e["type"] == kind]


def asked(ctx: PersonalRunContext) -> None:
    assert tools.tool_ask_aldo_size_assist(ctx, "Stessy", deadline_date="2026-10-17")["ok"]


def propose(ctx: PersonalRunContext, **overrides: Any) -> dict[str, Any]:
    args = {
        "size_request_id": "SZR-3F9A1C",
        "option_id": "SO-10",
        "chosen_size": "8.0",
        "rationale_public": "Runs small per ALDO fit signal; pickup meets Saturday",
        "confidence": "high",
    }
    args.update(overrides)
    return tools.tool_send_size_decision(ctx, **args)


# ------------------------------------------------------------------------------- recall


def test_recall_returns_the_owners_private_memory(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    result = tools.tool_recall_size_memory(ctx)
    assert result["ok"] and result["provenance"] == "synthetic"
    data = result["data"]
    assert data["typical_size"] == "7.5" and data["size_system"] == "US_W" and data["aldo_updates"] == []
    assert [(e["retailer"], e["size"], e["outcome"]) for e in data["entries"]] == [
        ("Retailer A (synthetic)", "7.5", "kept"),
        ("Retailer B (synthetic)", "7.5", "kept"),
        ("Retailer C (synthetic)", "8.0", "returned_too_big"),
    ]
    result_event = events(ctx, "tool_result")[0]["payload"]
    assert result_event["evidence"] == {"typical_size": "7.5", "entries": 3, "aldo_updates": 0}


def test_recall_filters_by_retailer(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    data = tools.tool_recall_size_memory(ctx, "Retailer B")["data"]
    assert [e["memory_id"] for e in data["entries"]] == ["PM-2"]


def test_recall_shows_active_aldo_updates_with_their_confidence(repo: MemoryRepo) -> None:
    repo.apply_update(
        {
            "update_id": "MU-5C0A11", "owner_ref": AVA, "order_id": "DEMO-A2A-81B20E", "style_id": "DEMO-STYLE-HERO",
            "product_name": "Stessy2.0", "color": "BLACK", "original_size": "8.0", "new_size": "8.5",
            "reason": "TOO_SMALL", "outcome": "exchange_created", "provisional": True, "source": "ALDO_CA",
            "ts": "2026-10-16T16:05:00+00:00",
        }
    )
    updates = tools.tool_recall_size_memory(make_ctx(repo))["data"]["aldo_updates"]
    assert updates == [
        {
            "update_id": "MU-5C0A11", "product_name": "Stessy2.0", "color": "BLACK", "original_size": "8.0",
            "new_size": "8.5", "reason": "TOO_SMALL", "outcome": "exchange_created", "confidence": "provisional",
        }
    ]


def test_recall_for_an_owner_without_a_profile_is_an_error_envelope(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo, owner="DEMO-CUST-NOBODY")
    result = tools.tool_recall_size_memory(ctx)
    assert result["ok"] is False and result["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------------- ask


def test_ask_returns_the_trimmed_answer_and_indexes_what_was_offered(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    result = tools.tool_ask_aldo_size_assist(ctx, "Stessy", color="black", deadline_date="2026-10-17")
    data = result["data"]
    assert (data["size_request_id"], data["product_name"], data["color"], data["price_cents"]) == (
        "SZR-3F9A1C", "Stessy2.0", "BLACK", 12999,
    )
    assert data["fit_signal"] == {
        "signal": "runs_small", "too_small_share": 0.799, "n_fit": 582, "scope": "ALDO_CA", "provenance": "derived",
    }
    assert [entry["size"] for entry in data["sizes"]] == ["7.0", "7.5", "8.0", "8.5"]
    assert result["provenance"] == "derived" and result["source_refs"] == ["SIZEREQ#SZR-3F9A1C"]
    assert ctx.last_size_request_id == "SZR-3F9A1C"
    assert set(ctx.offered["SZR-3F9A1C"]["sizes"]["8.0"]) == {"SO-8", "SO-9", "SO-10"}
    request = ctx.gateway.assist_requests[0]
    assert request == {
        "intent": "size_assist", "product_query": "Stessy", "size_system": "US_W", "color": "black",
        "deadline_date": "2026-10-17",
    }


def test_ask_traces_the_request_for_the_owner_panel(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    kinds = [e["type"] for e in events(ctx)]
    assert kinds == ["tool_call", "size_assist_requested", "tool_result"]
    requested = events(ctx, "size_assist_requested")[0]
    assert requested["actor"] == "personal_agent"
    assert requested["payload"] == {"product_query": "Stessy", "deadline_date": "2026-10-17"}
    evidence = events(ctx, "tool_result")[0]["payload"]["evidence"]
    assert evidence["signal"] == "runs_small" and evidence["n_fit"] == 582 and evidence["sizes_offered"] == 4


@pytest.mark.parametrize(
    "kwargs",
    [{"product_query": "x"}, {"product_query": "  "}, {"product_query": "Stessy", "deadline_date": "17/10/2026"},
     {"product_query": "Stessy", "deadline_date": "2026-13-45"}, {"product_query": "Stessy", "channel_pref": "drone"}],
)
def test_ask_validates_its_arguments_before_calling_aldo(repo: MemoryRepo, kwargs: dict[str, Any]) -> None:
    ctx = make_ctx(repo)
    result = tools.tool_ask_aldo_size_assist(ctx, **kwargs)
    assert result["ok"] is False and result["error"]["code"] == "VALIDATION_ERROR"
    assert ctx.gateway.assist_requests == []


def test_ask_sends_the_channel_preference_when_given(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    tools.tool_ask_aldo_size_assist(ctx, "Stessy", channel_pref="store_pickup")
    assert ctx.gateway.assist_requests[0]["channel_pref"] == "store_pickup"


def test_the_size_system_comes_from_the_owners_profile_not_from_the_model(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    ctx.size_system = "US_M"
    tools.tool_ask_aldo_size_assist(ctx, "Stessy")
    assert ctx.gateway.assist_requests[0]["size_system"] == "US_M"


# ------------------------------------------------------------------------------- send


def test_rationale_sanitized(repo: MemoryRepo) -> None:
    gateway = FakeGateway()
    ctx = make_ctx(repo, gateway)
    asked(ctx)
    raw = (
        "Retailer C (synthetic) sneaker 8.0 was too big, Retailer A running shoe 7.5 kept; "
        "ALDO says 80% of 582 returns say too small so half a size up from my usual 7.5, pickup meets Saturday"
    )
    result = propose(ctx, rationale_public=raw)
    assert result["ok"] is True
    sent = gateway.decision_requests[0]["rationale_public"]
    assert len(sent) <= 120
    for leak in ("Retailer", "synthetic", "sneaker", "running shoe", "7.5", "8.0"):
        assert leak not in sent
    assert "ALDO says 80%" in sent
    assert result["data"]["rationale_sent"] == sent
    # the owner-facing event shows what ALDO really received
    recommended = events(ctx, "size_recommended")[0]["payload"]
    assert recommended["rationale_public"] == sent


def test_a_long_rationale_is_truncated_to_120_characters(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    propose(ctx, rationale_public="runs small per ALDO fit signal " * 20)
    assert len(ctx.gateway.decision_requests[0]["rationale_public"]) <= 120


def test_a_rationale_that_is_only_private_data_falls_back_to_a_neutral_line(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    propose(ctx, rationale_public="Retailer C 8.0 sneaker")
    assert ctx.gateway.decision_requests[0]["rationale_public"] == tools.DEFAULT_RATIONALE


def test_the_decision_sent_to_aldo_has_exactly_the_documented_fields(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    propose(ctx, chosen_size=" 8 ")
    assert ctx.gateway.decision_requests[0] == {
        "size_request_id": "SZR-3F9A1C",
        "option_id": "SO-10",
        "chosen_size": "8.0",
        "rationale_public": "Runs small per ALDO fit signal; pickup meets Saturday",
        "confidence": "high",
    }


def test_the_result_is_a_proposal_and_says_so(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    data = propose(ctx)["data"]
    assert (data["status"], data["awaiting"]) == ("proposed", "owner_confirmation")
    assert "Nothing is ordered until the owner" in data["note"] and "order_id" not in data
    assert data["summary"]["size"] == "8.0" and data["summary"]["location_name"] == "ALDO Demo Store Montreal"
    assert ctx.proposal_sent is True
    assert ctx.proposal["size"] == "8.0" and ctx.proposal["option_type"] == "store_pickup"
    assert ctx.proposal["product_name"] == "Stessy2.0" and ctx.proposal["meets_deadline"] is True


def test_the_recommendation_is_traced_with_its_option_details(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    propose(ctx)
    payload = events(ctx, "size_recommended")[0]["payload"]
    assert events(ctx, "size_recommended")[0]["actor"] == "personal_agent"
    assert payload["chosen_size"] == "8.0" and payload["option_id"] == "SO-10"
    assert payload["option_type"] == "store_pickup" and payload["location_name"] == "ALDO Demo Store Montreal"
    assert payload["eta_date"] == "2026-10-17" and payload["meets_deadline"] is True
    assert payload["confidence"] == "high" and payload["status"] == "proposed"


def test_only_offered_sizes_and_options_can_be_proposed(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    for bad in (
        {"chosen_size": "9.0", "option_id": "SO-10"},  # size not offered
        {"chosen_size": "8.0", "option_id": "SO-5"},  # option belongs to size 7.5
        {"chosen_size": "8.0", "option_id": "SO-999"},  # option does not exist
    ):
        result = propose(ctx, **bad)
        assert result["error"]["code"] == "INVALID_SIZE_CHOICE", bad
    assert ctx.gateway.decision_requests == [] and ctx.proposal_sent is False


def test_a_request_from_an_earlier_turn_is_left_for_aldo_to_validate(repo: MemoryRepo) -> None:
    gateway = FakeGateway(decision=proposed_response("SZR-OLD001", "8.0"))
    ctx = make_ctx(repo, gateway)
    result = propose(ctx, size_request_id="SZR-OLD001")  # this run never asked, so there is no local index
    assert result["ok"] is True and gateway.decision_requests[0]["size_request_id"] == "SZR-OLD001"


@pytest.mark.parametrize(
    "bad,code",
    [
        ({"confidence": "certain"}, "VALIDATION_ERROR"),
        ({"size_request_id": "bad id!"}, "VALIDATION_ERROR"),
        ({"option_id": ""}, "VALIDATION_ERROR"),
        ({"chosen_size": "eight"}, "INVALID_SIZE_CHOICE"),
        ({"chosen_size": "8.25"}, "INVALID_SIZE_CHOICE"),
        ({"chosen_size": "-8.0"}, "INVALID_SIZE_CHOICE"),
    ],
)
def test_malformed_proposals_are_rejected_locally(repo: MemoryRepo, bad: dict[str, Any], code: str) -> None:
    ctx = make_ctx(repo)
    asked(ctx)
    result = propose(ctx, **bad)
    assert result["error"]["code"] == code and ctx.gateway.decision_requests == []


@pytest.mark.parametrize("given,expected", [("8", "8.0"), ("8.0", "8.0"), (" 7.50 ", "7.5"), ("10", "10.0")])
def test_sizes_are_normalized_to_one_decimal(given: str, expected: str) -> None:
    assert tools.normalize_size(given) == expected


def test_a_stale_request_is_reported_so_the_model_can_ask_again(repo: MemoryRepo) -> None:
    from personal_agent.gateway_client import GatewayCallError

    ctx = make_ctx(repo, FakeGateway(decision=GatewayCallError("REQUEST_EXPIRED", "expired")))
    asked(ctx)
    result = propose(ctx)
    assert result["error"]["code"] == "REQUEST_EXPIRED" and ctx.proposal_sent is False
    assert events(ctx, "size_recommended") == []


def test_the_proposal_can_be_replaced_by_a_newer_one(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo, FakeGateway(decision=proposed_response("SZR-3F9A1C", "8.0")))
    asked(ctx)
    assert propose(ctx)["ok"] and propose(ctx, option_id="SO-9")["ok"]
    assert len(ctx.gateway.decision_requests) == 2


# ------------------------------------------------------------------------------- record


def test_record_decision_is_an_owner_facing_event(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    result = tools.tool_record_decision(ctx, "Size 8.0 (usual 7.5 + half size, style runs small), Montreal pickup")
    assert result["ok"] and result["data"] == {"recorded": True}
    decision = events(ctx, "decision")[0]
    assert decision["actor"] == "personal_agent"
    assert decision["payload"]["summary"].startswith("Size 8.0 (usual 7.5")


def test_record_decision_is_capped_at_240_characters(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    tools.tool_record_decision(ctx, "word " * 200)
    assert len(events(ctx, "decision")[0]["payload"]["summary"]) <= 240


def test_record_decision_rejects_an_empty_line(repo: MemoryRepo) -> None:
    ctx = make_ctx(repo)
    result = tools.tool_record_decision(ctx, "  \n ")
    assert result["error"]["code"] == "VALIDATION_ERROR" and events(ctx, "decision") == []


# --------------------------------------------------------------------- public sanitizer API


def test_sanitize_public_rationale_accepts_the_owner_memory_model(repo: MemoryRepo) -> None:
    owner_memory = repo.get_owner_memory(AVA)
    text = "Retailer B loafer in 7.5 fit; ALDO says it runs small"
    cleaned = tools.sanitize_public_rationale(text, owner_memory)
    assert cleaned == "in fit; ALDO says it runs small"
    short = tools.sanitize_public_rationale(text, owner_memory.model_dump(), 12)  # the dict form works too
    assert len(short) <= 12 and short.startswith("in fit")


def test_sanitize_public_rationale_never_raises() -> None:
    assert tools.sanitize_public_rationale("anything", None).startswith("Chosen from")  # type: ignore[arg-type]
