"""Money helpers: integer cents, ISO currency CAD, never floats (requirement 12.5)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import DomainError, ErrorCode

CURRENCY = "CAD"


def ensure_cents(value: object, field: str = "amount", *, minimum: int | None = None) -> int:
    """Return ``value`` as an int number of cents or raise VALIDATION_ERROR.

    Accepts ints and integral ``Decimal`` values (what boto3 returns for numbers). Rejects
    bools, floats and strings so a float can never sneak into a persisted amount.
    """
    if isinstance(value, bool):
        raise _not_cents(field)
    if isinstance(value, int):
        cents = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        cents = int(value)
    else:
        raise _not_cents(field)
    if minimum is not None and cents < minimum:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"{field} must be at least {minimum} cents")
    return cents


def dollars_to_cents(value: str | Decimal) -> int:
    """Parse a dollar amount such as ``"129.99"`` into cents (half-up). Floats are rejected."""
    if not isinstance(value, (str, Decimal)):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "dollar amounts must be a string or Decimal")
    try:
        amount = Decimal(value.strip() if isinstance(value, str) else value)
        if not amount.is_finite():
            raise InvalidOperation
        return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except ArithmeticError:  # InvalidOperation for text like "abc", Overflow for "1e999999"
        raise DomainError(ErrorCode.VALIDATION_ERROR, "not a valid dollar amount") from None


def format_cad(cents: int) -> str:
    """``12999`` -> ``"CAD 129.99"``; thousands are grouped, negatives read ``-CAD 5.95``."""
    amount = ensure_cents(cents, "cents")
    sign = "-" if amount < 0 else ""
    whole, fraction = divmod(abs(amount), 100)
    return f"{sign}{CURRENCY} {whole:,}.{fraction:02d}"


def _not_cents(field: str) -> DomainError:
    return DomainError(ErrorCode.VALIDATION_ERROR, f"{field} must be an integer number of cents")
