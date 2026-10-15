from __future__ import annotations

import copy
import pickle

import pytest

from returns_domain.errors import (
    ALL_CODES,
    HTTP_STATUS,
    RETRYABLE_CODES,
    DomainError,
    ErrorCode,
)

REQUIREMENT_12_3_CODES = {
    "ORDER_NOT_FOUND",
    "NOT_AUTHORIZED",
    "MISSING_POLICY",
    "QUOTE_EXPIRED",
    "STOCK_CHANGED",
    "APPROVAL_REQUIRED",
    "IDEMPOTENCY_CONFLICT",
    "UPSTREAM_TIMEOUT",
    "LIMIT_REACHED",
    "VALIDATION_ERROR",
}


def test_all_requirement_12_3_codes_exist():
    assert REQUIREMENT_12_3_CODES <= ALL_CODES


def test_authentication_and_design_gap_codes_exist():
    # R1.3, design-gap G-04 (TransactionConflict) and G-06 (one active run per conversation)
    assert {"NOT_AUTHENTICATED", "TRANSACTION_CONFLICT", "RUN_IN_PROGRESS"} <= ALL_CODES
    assert {"NOT_FOUND", "INTERNAL_ERROR"} <= ALL_CODES


def test_fit_gateway_pivot_codes_exist():
    # PIVOT_ADDENDUM C4, C7, 5.2, 5.3
    assert {
        "INVALID_SIZE_CHOICE",
        "UNKNOWN_STYLE",
        "REQUEST_EXPIRED",
        "DELEGATION_LIMIT_EXCEEDED",
    } <= ALL_CODES


def test_every_code_maps_to_an_http_status():
    assert set(HTTP_STATUS) == ALL_CODES


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("NOT_AUTHENTICATED", 401),
        ("NOT_AUTHORIZED", 403),
        ("ORDER_NOT_FOUND", 404),
        ("NOT_FOUND", 404),
        ("QUOTE_EXPIRED", 409),
        ("STOCK_CHANGED", 409),
        ("APPROVAL_REQUIRED", 409),
        ("IDEMPOTENCY_CONFLICT", 409),
        ("RUN_IN_PROGRESS", 409),
        ("TRANSACTION_CONFLICT", 409),
        ("LIMIT_REACHED", 429),
        ("VALIDATION_ERROR", 422),
        ("INVALID_SIZE_CHOICE", 422),  # PIVOT_ADDENDUM 5.3
        ("UNKNOWN_STYLE", 422),
        ("REQUEST_EXPIRED", 409),
        ("DELEGATION_LIMIT_EXCEEDED", 403),
        ("UPSTREAM_TIMEOUT", 504),
        ("INTERNAL_ERROR", 500),
    ],
)
def test_http_status_follows_design_section_7(code: str, status: int):
    assert DomainError(code, "x").http_status == status


def test_unknown_code_is_a_500():
    assert DomainError("SOMETHING_ELSE", "x").http_status == 500


def test_to_dict_is_the_tool_error_object():
    error = DomainError(ErrorCode.STOCK_CHANGED, "Stock changed", retryable=False)
    assert error.to_dict() == {
        "code": "STOCK_CHANGED",
        "message": "Stock changed",
        "retryable": False,
    }


def test_enum_and_plain_string_codes_are_equivalent():
    assert DomainError(ErrorCode.QUOTE_EXPIRED, "m").code == DomainError("QUOTE_EXPIRED", "m").code
    assert type(DomainError(ErrorCode.QUOTE_EXPIRED, "m").code) is str


def test_retryable_defaults_from_the_code_and_can_be_overridden():
    assert RETRYABLE_CODES == {"UPSTREAM_TIMEOUT", "TRANSACTION_CONFLICT", "RUN_IN_PROGRESS"}
    assert DomainError("UPSTREAM_TIMEOUT", "m").retryable is True
    assert DomainError("TRANSACTION_CONFLICT", "m").retryable is True
    assert DomainError("STOCK_CHANGED", "m").retryable is False
    assert DomainError("NOT_AUTHORIZED", "m").retryable is False
    assert DomainError("STOCK_CHANGED", "m", retryable=True).retryable is True
    assert DomainError("UPSTREAM_TIMEOUT", "m", retryable=False).retryable is False


def test_str_and_exception_behaviour():
    error = DomainError("ORDER_NOT_FOUND", "No such order")
    assert str(error) == "ORDER_NOT_FOUND: No such order"
    with pytest.raises(DomainError) as caught:
        raise error
    assert caught.value.code == "ORDER_NOT_FOUND"


def test_error_survives_copy_and_pickle():
    original = DomainError(
        "UNKNOWN_STYLE", "Several products match", details={"candidates": ["A", "B"]}
    )
    for clone in (copy.copy(original), pickle.loads(pickle.dumps(original))):
        assert clone.to_dict() == original.to_dict()
        assert clone.details == {"candidates": ["A", "B"]}
        assert clone.http_status == 422


def test_details_are_optional_copied_and_only_shown_when_set():
    plain = DomainError("NOT_FOUND", "No such quote")
    assert plain.details == {}
    assert "details" not in plain.to_dict()

    source = {"candidates": ["Stessy2.0"]}
    with_details = DomainError("UNKNOWN_STYLE", "Several", details=source)
    source["candidates"] = ["changed later"]
    assert with_details.to_dict() == {
        "code": "UNKNOWN_STYLE",
        "message": "Several",
        "retryable": False,
        "details": {"candidates": ["Stessy2.0"]},
    }
    # The returned dict is a copy: editing it does not change the error.
    with_details.to_dict()["details"]["candidates"].append("x")
    assert with_details.details == {"candidates": ["Stessy2.0"]}
