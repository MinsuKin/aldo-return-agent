"""Outbound text sanitizer (PIVOT_ADDENDUM C3, C7). Pure functions, no I/O.

Everything the personal agent sends to ALDO in free text (``rationale_public``, the product
query) passes through ``sanitize_public_text`` first. It removes, in this order:

1. every retailer label found in the owner's memory (case-insensitive, with and without the
   "(synthetic)" marker) and any generic "Retailer X" label;
2. the item names of the owner's other purchases (whole-phrase match);
3. every number that appears in the owner's memory (typical size and sizes of other purchases),
   compared by numeric value, so "8", "8.0", "8,0" and "7 ½" are the same numbers as 8.0 and 7.5;
   spelled-out forms ("seven and a half", "eight") are removed as well;

then tidies leftover punctuation and truncates to the limit (default 120 characters). The model is
also told not to include any of this; this module is the backstop that does not depend on it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_GENERIC_RETAILER = re.compile(r"(?<!\w)retailer\s+[a-z0-9](?!\w)", re.IGNORECASE)
_SYNTHETIC_MARK = re.compile(r"\(\s*synthetic\s*\)", re.IGNORECASE)
_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
_REPEATED_PUNCT = re.compile(r"([,;:])(?:\s*[,;:])+")
_WHITESPACE = re.compile(r"\s+")
_HALF_FRACTION = re.compile(r"(\d)\s*[\u00bd]")
_NUMBER_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty"
).split()
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MD_BULLET = re.compile(r"(?m)^\s*(?:[-*\u2022]|\d{1,2}[.)])\s+")
_MD_EMPHASIS = re.compile(r"\*\*|__|\*|`")


#: Words that are never stripped even if they show up as a memory label or item.
_PROTECTED_TERMS = frozenset({"aldo", "aldo bot"})


def is_protected(term: str) -> bool:
    """True for terms the sanitizer must keep (ALDO is the counterparty, not another retailer)."""
    return normalize_text(term).lower() in _PROTECTED_TERMS


@dataclass(frozen=True)
class PrivateTerms:
    """What must never leave in free text: retailers, other purchases' items and their numbers."""

    retailers: tuple[str, ...] = ()
    items: tuple[str, ...] = ()
    numbers: frozenset[Decimal] = frozenset()

    @classmethod
    def from_memory(cls, view: Mapping[str, Any]) -> "PrivateTerms":
        """Build from a ``memory.list_memory`` view (typical size plus the memory entries)."""
        retailers: list[str] = []
        items: list[str] = []
        numbers: set[Decimal] = set()
        _add_number(numbers, view.get("typical_size"))
        for entry in view.get("memory") or []:
            if not isinstance(entry, Mapping):
                continue
            retailer = entry.get("retailer")
            if isinstance(retailer, str) and retailer.strip():
                retailers.append(retailer.strip())
            item = entry.get("item")
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            _add_number(numbers, entry.get("size"))
        return cls(
            retailers=tuple(dict.fromkeys(retailers)),
            items=tuple(dict.fromkeys(items)),
            numbers=frozenset(numbers),
        )

    def label_variants(self) -> list[str]:
        """Retailer labels plus the same labels without a trailing parenthetical, longest first.

        The bare word ALDO is never a variant: ALDO is the counterparty of the conversation, so it
        must survive even if a memory label were (mistakenly) set to it.
        """
        variants: set[str] = set()
        for label in self.retailers:
            variants.add(label)
            bare = _TRAILING_PAREN.sub("", label).strip()
            if bare:
                variants.add(bare)
        return sorted((v for v in variants if not is_protected(v)), key=len, reverse=True)


def normalize_text(text: object) -> str:
    """NFKC-normalize, drop invisible/control characters and collapse whitespace."""
    raw = _HALF_FRACTION.sub(r"\1.5", str(text if text is not None else ""))
    raw = unicodedata.normalize("NFKC", raw)
    kept: list[str] = []
    for char in raw:
        category = unicodedata.category(char)
        if char in "\n\r\t" or category == "Zs":
            kept.append(" ")
        elif category in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            continue
        else:
            kept.append(char)
    return _WHITESPACE.sub(" ", "".join(kept)).strip()


def truncate(text: str, max_chars: int) -> str:
    """Hard cap at ``max_chars``; cut at a word boundary when one is close enough."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = cut.rfind(" ")
    if boundary >= int(max_chars * 0.6):
        cut = cut[:boundary]
    return cut.rstrip(" ,;:-")


def limit_words(text: str, max_words: int) -> str:
    """Keep at most ``max_words`` words (whitespace separated).

    When the text is cut it ends on the last complete sentence if most of the text survives that
    way; otherwise an ellipsis marks the cut.
    """
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    cut = " ".join(words[:max_words])
    if cut.endswith((".", "!", "?")):
        return cut
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if boundary >= int(len(cut) * 0.6):
        return cut[: boundary + 1]
    return cut.rstrip(" ,;:-") + "…"


def strip_markdown(text: str) -> str:
    """Plain text for the chat: no headings, list markers, bold/italic stars, underscores pairs or backticks.

    The UI shows the reply as plain text, so markdown would appear as stray asterisks.
    """
    cleaned = _MD_HEADING.sub("", text)
    cleaned = _MD_BULLET.sub("", cleaned)
    return _MD_EMPHASIS.sub("", cleaned).strip()


def count_words(text: str) -> int:
    return len(text.split())


def spelled_forms(number: Decimal) -> list[str]:
    """Spelled-out ways to write a shoe size: "seven and a half", "seven point five", "eight", "8 1/2"."""
    if number < 0 or number > 20:
        return []
    whole = int(number)
    fraction = number - whole
    if fraction == 0:
        return [_NUMBER_WORDS[whole]]
    if fraction == Decimal("0.5"):
        word = _NUMBER_WORDS[whole]
        return [f"{word} and a half", f"{word}-and-a-half", f"{word} point five", f"{word} 1/2", f"{whole} 1/2", f"{whole}-1/2"]
    return []


def _spelled_patterns(numbers: Iterable[Decimal]) -> list[re.Pattern[str]]:
    phrases = sorted({form for number in numbers for form in spelled_forms(number)}, key=len, reverse=True)
    return [_phrase_pattern(phrase) for phrase in phrases]


def sanitize_public_text(text: object, terms: PrivateTerms, *, max_chars: int) -> str:
    """Return ``text`` without private terms, tidied and at most ``max_chars`` long."""
    cleaned = normalize_text(text)
    for label in terms.label_variants():
        cleaned = _phrase_pattern(label).sub(" ", cleaned)
    cleaned = _GENERIC_RETAILER.sub(" ", cleaned)
    cleaned = _SYNTHETIC_MARK.sub(" ", cleaned)
    for item in sorted(terms.items, key=len, reverse=True):
        if not is_protected(item):
            cleaned = _phrase_pattern(item).sub(" ", cleaned)
    for pattern in _spelled_patterns(terms.numbers):
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _strip_numbers(cleaned, terms.numbers)
    return truncate(_tidy(cleaned), max_chars)


_WORD = re.compile(r"\w+(?:'\w+)?", re.UNICODE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
QUOTE_WINDOW = 4  # consecutive words that count as "quoting" a source


def _word_grams(text: object, window: int) -> set[tuple[str, ...]]:
    words = _WORD.findall(normalize_text(text).lower())
    return {tuple(words[index : index + window]) for index in range(len(words) - window + 1)}


def quotes_any(text: object, sources: Iterable[str], *, window: int = QUOTE_WINDOW) -> bool:
    """True when ``text`` repeats ``window`` or more consecutive words of any of ``sources``.

    Used for flagged reviews: whatever an injected review said must not come back out in a public
    rationale or in the reply to the owner, not even as a quotation. Case, punctuation and
    spacing do not matter.
    """
    grams = _word_grams(text, window)
    if not grams:
        return False
    return any(grams & _word_grams(source, window) for source in sources if source)


def drop_quoting_sentences(text: str, sources: Iterable[str], *, window: int = QUOTE_WINDOW) -> str:
    """``text`` without the sentences that quote a source (the rest is kept as written)."""
    sources = [source for source in sources if source]
    if not sources:
        return text
    kept = [
        sentence
        for sentence in _SENTENCE_END.split(text.strip())
        if sentence and not quotes_any(sentence, sources, window=window)
    ]
    return " ".join(kept).strip()


def contains_private_term(text: str, terms: PrivateTerms) -> bool:
    """True if ``text`` still contains a retailer label, other-purchase item or private number."""
    probe = normalize_text(text)
    if _GENERIC_RETAILER.search(probe) or _SYNTHETIC_MARK.search(probe):
        return True
    if any(_phrase_pattern(label).search(probe) for label in terms.label_variants()):
        return True
    if any(_phrase_pattern(item).search(probe) for item in terms.items if not is_protected(item)):
        return True
    if any(pattern.search(probe) for pattern in _spelled_patterns(terms.numbers)):
        return True
    return any(
        _number_value(match.group(0)) in terms.numbers for match in _NUMBER.finditer(probe)
    )


# ----------------------------------------------------------------------------- internals


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in normalize_text(phrase).split(" ") if part]
    body = r"\s+".join(parts) if parts else r"(?!)"
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _number_value(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "."))
    except InvalidOperation:
        return None


def _add_number(target: set[Decimal], value: object) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, Decimal)):
        target.add(Decimal(value))
        return
    if isinstance(value, str):
        for match in _NUMBER.finditer(value):
            number = _number_value(match.group(0))
            if number is not None:
                target.add(number)


def _strip_numbers(text: str, numbers: Iterable[Decimal]) -> str:
    blocked = frozenset(numbers)
    if not blocked:
        return text

    def replace(match: re.Match[str]) -> str:
        return " " if _number_value(match.group(0)) in blocked else match.group(0)

    return _NUMBER.sub(replace, text)


def _tidy(text: str) -> str:
    tidy = _EMPTY_BRACKETS.sub(" ", text)
    tidy = _SPACE_BEFORE_PUNCT.sub(r"\1", tidy)
    tidy = _REPEATED_PUNCT.sub(r"\1", tidy)
    tidy = _WHITESPACE.sub(" ", tidy).strip()
    return tidy.strip(" ,;:-–—")
