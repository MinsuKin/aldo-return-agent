from __future__ import annotations

from decimal import Decimal

import pytest

from returns_domain import money
from returns_domain.errors import DomainError


def test_currency_is_cad():
    assert money.CURRENCY == "CAD"


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "CAD 0.00"),
        (5, "CAD 0.05"),
        (595, "CAD 5.95"),
        (5999, "CAD 59.99"),
        (12999, "CAD 129.99"),
        (25000, "CAD 250.00"),
        (28900, "CAD 289.00"),
        (129900, "CAD 1,299.00"),
        (100_000_000, "CAD 1,000,000.00"),
        (-595, "-CAD 5.95"),
    ],
)
def test_format_cad(cents: int, text: str):
    assert money.format_cad(cents) == text


@pytest.mark.parametrize("bad", [12.5, 12999.0, True, False, "12999", None, [1], Decimal("12.5")])
def test_ensure_cents_rejects_everything_that_is_not_whole_cents(bad: object):
    with pytest.raises(DomainError) as caught:
        money.ensure_cents(bad, "refund_cents")
    assert caught.value.code == "VALIDATION_ERROR"
    assert "refund_cents" in caught.value.message


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_ensure_cents_rejects_non_finite_decimals(bad: Decimal):
    with pytest.raises(DomainError):
        money.ensure_cents(bad)


def test_ensure_cents_accepts_ints_and_integral_decimals_from_boto3():
    assert money.ensure_cents(12999) == 12999
    assert money.ensure_cents(0) == 0
    assert money.ensure_cents(Decimal("12999")) == 12999
    result = money.ensure_cents(Decimal("12999"))
    assert type(result) is int


def test_ensure_cents_minimum():
    assert money.ensure_cents(0, minimum=0) == 0
    with pytest.raises(DomainError):
        money.ensure_cents(-1, "fee_cents", minimum=0)


def test_format_cad_refuses_floats():
    with pytest.raises(DomainError):
        money.format_cad(129.99)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("129.99", 12999),
        ("5.95", 595),
        ("0.1", 10),
        (" 12 ", 1200),
        ("1150050.58", 115_005_058),
        ("0.005", 1),  # half up
        ("0.004", 0),
        ("19.995", 2000),
        (Decimal("79.99"), 7999),
    ],
)
def test_dollars_to_cents(text: str | Decimal, cents: int):
    result = money.dollars_to_cents(text)
    assert result == cents
    assert type(result) is int


@pytest.mark.parametrize("bad", [1.5, 5, None, "abc", "", "NaN", "Infinity", "1e999999"])
def test_dollars_to_cents_rejects_floats_ints_and_garbage(bad: object):
    with pytest.raises(DomainError) as caught:
        money.dollars_to_cents(bad)  # type: ignore[arg-type]
    assert caught.value.code == "VALIDATION_ERROR"
