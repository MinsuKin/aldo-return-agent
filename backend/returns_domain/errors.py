"""Domain error type and the machine-readable error codes.

Codes: requirement 12.3, NOT_AUTHENTICATED (requirement 1.3), plus the extras the
design-gap review needs: RUN_IN_PROGRESS (G-06), TRANSACTION_CONFLICT (G-04),
NOT_FOUND (quote/run/transaction/case that does not exist) and INTERNAL_ERROR, and the
Fit Gateway pivot codes INVALID_SIZE_CHOICE, UNKNOWN_STYLE, REQUEST_EXPIRED and
DELEGATION_LIMIT_EXCEEDED.
"""

from __future__ import annotations

import copy
from enum import StrEnum
from typing import Any, Mapping


class ErrorCode(StrEnum):
    # Requirement 12.3
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    MISSING_POLICY = "MISSING_POLICY"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    STOCK_CHANGED = "STOCK_CHANGED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    LIMIT_REACHED = "LIMIT_REACHED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # Requirement 1.3
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    # Design 7 / design-gap review
    RUN_IN_PROGRESS = "RUN_IN_PROGRESS"
    TRANSACTION_CONFLICT = "TRANSACTION_CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Fit Gateway pivot (PIVOT_ADDENDUM C4, C7, 5.2, 5.3)
    INVALID_SIZE_CHOICE = "INVALID_SIZE_CHOICE"
    UNKNOWN_STYLE = "UNKNOWN_STYLE"
    REQUEST_EXPIRED = "REQUEST_EXPIRED"
    DELEGATION_LIMIT_EXCEEDED = "DELEGATION_LIMIT_EXCEEDED"


ALL_CODES: frozenset[str] = frozenset(code.value for code in ErrorCode)

#: Codes whose default is "the caller may try the same call again".
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.UPSTREAM_TIMEOUT.value,
        ErrorCode.TRANSACTION_CONFLICT.value,
        ErrorCode.RUN_IN_PROGRESS.value,
    }
)

#: HTTP status the API layer returns for each code (design 7).
HTTP_STATUS: dict[str, int] = {
    ErrorCode.NOT_AUTHENTICATED.value: 401,
    ErrorCode.NOT_AUTHORIZED.value: 403,
    ErrorCode.ORDER_NOT_FOUND.value: 404,
    ErrorCode.NOT_FOUND.value: 404,
    ErrorCode.MISSING_POLICY.value: 409,
    ErrorCode.QUOTE_EXPIRED.value: 409,
    ErrorCode.STOCK_CHANGED.value: 409,
    ErrorCode.APPROVAL_REQUIRED.value: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT.value: 409,
    ErrorCode.RUN_IN_PROGRESS.value: 409,
    ErrorCode.TRANSACTION_CONFLICT.value: 409,
    ErrorCode.LIMIT_REACHED.value: 429,
    ErrorCode.VALIDATION_ERROR.value: 422,
    ErrorCode.UPSTREAM_TIMEOUT.value: 504,
    ErrorCode.INTERNAL_ERROR.value: 500,
    ErrorCode.INVALID_SIZE_CHOICE.value: 422,
    ErrorCode.UNKNOWN_STYLE.value: 422,
    ErrorCode.REQUEST_EXPIRED.value: 409,
    ErrorCode.DELEGATION_LIMIT_EXCEEDED.value: 403,
}


class DomainError(Exception):
    """A business or validation failure with a stable machine-readable code.

    ``retryable`` defaults from the code (see ``RETRYABLE_CODES``) when not given.
    ``details`` is optional, small, JSON-safe data for the caller (for example the
    ``candidates`` of UNKNOWN_STYLE). Never put secrets or customer identifiers in it.
    """

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        resolved_code = str(code)
        resolved_retryable = (
            resolved_code in RETRYABLE_CODES if retryable is None else bool(retryable)
        )
        super().__init__(resolved_code, message, resolved_retryable)
        self.code: str = resolved_code
        self.message: str = message
        self.retryable: bool = resolved_retryable
        self.details: dict[str, Any] = copy.deepcopy(dict(details)) if details else {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def to_dict(self) -> dict[str, Any]:
        """The ``error`` object of the tool and API envelopes (``details`` only when set)."""
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            body["details"] = copy.deepcopy(self.details)
        return body

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
