"""Reason taxonomy and the other closed vocabularies the agent passes to tools.

The model supplies ``reason_category`` and ``condition`` as enum values (requirement 3.1);
``parse_*`` validates them strictly, so free text can never reach the policy.
``categorize`` is a deterministic helper for raw return reasons (the CSV has 47 distinct
spellings, some French or truncated) and for keyword-level triage of free text.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TypeVar

from .errors import DomainError, ErrorCode


class ReasonCategory(StrEnum):
    FIT_SIZE = "FIT_SIZE"
    COMFORT = "COMFORT"
    QUALITY_DAMAGE = "QUALITY_DAMAGE"
    EXPECTATION = "EXPECTATION"
    CHANGE_OF_MIND = "CHANGE_OF_MIND"
    DELIVERY = "DELIVERY"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ItemCondition(StrEnum):
    UNWORN = "unworn"
    WORN = "worn"
    DAMAGED = "damaged"
    UNKNOWN = "unknown"


class SizeDirection(StrEnum):
    """Size problem direction (design-gap G-14); decides the default replacement size."""

    TOO_SMALL = "too_small"
    TOO_BIG = "too_big"
    UNKNOWN = "unknown"


REASON_CATEGORIES: tuple[str, ...] = tuple(c.value for c in ReasonCategory)
CONDITIONS: tuple[str, ...] = tuple(c.value for c in ItemCondition)
SIZE_DIRECTIONS: tuple[str, ...] = tuple(c.value for c in SizeDirection)

_E = TypeVar("_E", bound=StrEnum)


def _parse_enum(enum_type: type[_E], value: object, label: str) -> _E:
    allowed = ", ".join(member.value for member in enum_type)
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str) or not value.strip():
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"{label} is required; use one of: {allowed}")
    wanted = value.strip().replace("-", "_").replace(" ", "_").casefold()
    for member in enum_type:
        if member.value.casefold() == wanted:
            return member
    raise DomainError(ErrorCode.VALIDATION_ERROR, f"{label} must be one of: {allowed}")


def parse_reason_category(value: object) -> ReasonCategory:
    return _parse_enum(ReasonCategory, value, "reason_category")


def parse_condition(value: object) -> ItemCondition:
    return _parse_enum(ItemCondition, value, "condition")


def parse_size_direction(value: object) -> SizeDirection:
    return _parse_enum(SizeDirection, value, "size_direction")


# --------------------------------------------------------------------------- categorize

_RAW_PREFIX = "return.page.reasons."

# Normalized (lower-case, prefix dropped) raw reasons seen in the OMS returns CSV.
_KNOWN_REASONS: dict[str, ReasonCategory] = {
    "too small": ReasonCategory.FIT_SIZE,
    "too big": ReasonCategory.FIT_SIZE,
    "uncomfortable": ReasonCategory.COMFORT,
    "defective": ReasonCategory.QUALITY_DAMAGE,
    "manufacturer defect": ReasonCategory.QUALITY_DAMAGE,
    "poor quality": ReasonCategory.QUALITY_DAMAGE,
    "arrived in poor condition": ReasonCategory.QUALITY_DAMAGE,
    "warranty": ReasonCategory.QUALITY_DAMAGE,
    "not what i expected": ReasonCategory.EXPECTATION,
    "not as advertised": ReasonCategory.EXPECTATION,
    "not satisfied with the colour": ReasonCategory.EXPECTATION,
    "changed idea": ReasonCategory.CHANGE_OF_MIND,
    "changed my mind / other": ReasonCategory.CHANGE_OF_MIND,
    "found something else": ReasonCategory.CHANGE_OF_MIND,
    "lost in transit": ReasonCategory.DELIVERY,
    "arrived too late": ReasonCategory.DELIVERY,
    "wrong item": ReasonCategory.DELIVERY,
    "wrong item delivered": ReasonCategory.DELIVERY,
    "wrong size delivered": ReasonCategory.DELIVERY,
    "missing item": ReasonCategory.DELIVERY,
    "return to sender": ReasonCategory.DELIVERY,
    "return without merchandise": ReasonCategory.OTHER,
    "return without merch": ReasonCategory.OTHER,
    "return extension": ReasonCategory.OTHER,
    "gift return": ReasonCategory.OTHER,
    "in-store return": ReasonCategory.OTHER,
}

# Keyword fallback, first match wins. Order matters: defects before delivery ("arrived in
# poor condition"), delivery before fit ("wrong size delivered"), expectation before
# change of mind ("don't like the colour"). Also covers the truncated and French spellings.
_KEYWORD_RULES: tuple[tuple[ReasonCategory, re.Pattern[str]], ...] = (
    (
        ReasonCategory.QUALITY_DAMAGE,
        re.compile(
            r"\b(defect\w*|broke|broken|crack\w*|torn|tore|rip\w*|peel\w*|damag\w*|faulty"
            r"|fell apart|came apart|came off|poor (quality|cond\w*)|warranty|mauvais)"
        ),
    ),
    (
        ReasonCategory.DELIVERY,
        re.compile(
            r"(transit|arrived (too )?late|late deliver\w*|never arrived|did ?n.?t arrive"
            r"|wrong (item|product)|wrong size (was )?(delivered|sent|shipped)|missing item"
            r"|return to sender)"
        ),
    ),
    (
        ReasonCategory.EXPECTATION,
        re.compile(
            r"\b(expect\w*|advertis\w*|as described|not as shown|looks? different"
            r"|different (from|than)|colou?r|ne r.pond pas|attentes|photos?|pictures?)\b"
        ),
    ),
    (
        ReasonCategory.CHANGE_OF_MIND,
        re.compile(
            r"(chang\w* (my|his|her|our) mind|changed idea|chang\w+ d.id.e"
            r"|(do ?n.?t|do not) (like|want)|no longer (want|need)"
            r"|found (something|a cheaper)|by mistake)"
        ),
    ),
    (
        ReasonCategory.COMFORT,
        re.compile(r"\b(\w*co[mn]fort\w*|hurts?|painful|pain|blisters?|pinch\w*|rubs?|sore)\b"),
    ),
    (
        ReasonCategory.FIT_SIZE,
        re.compile(
            r"(too (small|big|large|tight|loose|narrow|wide|short|long)|trop (grand|petit)"
            r"|\bsizes?\b|\bfit(s|ting)?\b|did ?n.?t fit|does ?n.?t fit)"
        ),
    ),
)


def _normalize(text: str) -> str:
    cleaned = " ".join(text.split()).casefold()
    return cleaned[len(_RAW_PREFIX) :] if cleaned.startswith(_RAW_PREFIX) else cleaned


def categorize(reason_text: str | None) -> ReasonCategory:
    """Map a raw return reason or short free text to a taxonomy category.

    Blank -> UNKNOWN; known CSV spellings map exactly; other text goes through the keyword
    rules; anything left is OTHER. Never used to decide policy: the model passes the enum.
    """
    if reason_text is None or not str(reason_text).strip():
        return ReasonCategory.UNKNOWN
    text = _normalize(str(reason_text))
    known = _KNOWN_REASONS.get(text)
    if known is not None:
        return known
    for category, pattern in _KEYWORD_RULES:
        if pattern.search(text):
            return category
    return ReasonCategory.OTHER
