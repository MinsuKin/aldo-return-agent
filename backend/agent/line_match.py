"""Deterministic matching of the owner's words to the lines of one order (David's bundle return).

The owner may name items the way people do ("the tan boot, the white sneaker and the navy loafer").
This module turns each phrase into a line of the order, using only the order data the domain returned:
colour first, then category, then product name. Nothing here is a model call and nothing is guessed:

* a phrase that fits exactly one returnable line is ``matched``;
* a phrase that fits several lines is ``ambiguous`` (the agent asks which one, listing ``candidates``);
* a phrase that fits no line, or only words the order does not contain, is ``none``;
* a second phrase for a line that is already matched is ``duplicate``.

A bare number ("5", "number 5", "#5", "item 5") means the position in the numbered list the owner was shown
(``number`` on each line). The matcher never reads a customer id, a price or a policy: it only chooses lines.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

MAX_PHRASES = 10
MAX_PHRASE_CHARS = 80

#: Words that carry no information about WHICH item (articles, "pair of", generic "shoes", filler).
STOP_WORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "my", "our", "pair", "pairs", "of", "shoe", "shoes", "one", "ones", "and", "or", "item",
        "items", "please", "also", "return", "returning", "these", "those", "this", "that", "with", "in", "size",
        "number", "no", "nr", "it", "them", "is", "are", "i", "to", "too",
    }
)
_NUMBER = re.compile(r"^\s*(?:#|no\.?|nr\.?|number|item|items)?\s*#?\s*(\d{1,2})\s*$", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z]+")


def _norm(word: str) -> str:
    """Lower-case letters only, a trailing plural "s" removed ("boots" and "boot" match)."""
    token = "".join(_TOKEN.findall(str(word).lower()))
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token


def _words(text: object) -> list[str]:
    return [_norm(w) for w in _TOKEN.findall(str(text or "").lower()) if _norm(w)]


def number_lines(lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The order lines in the order the owner is shown (L1, L2, ... L10) with ``number`` 1..n.

    The domain lists lines by id as text (L1, L10, L2, ...); the numbered list the owner sees follows the
    number in the line id, falling back to the original order for ids without one.
    """

    def key(pair: tuple[int, Mapping[str, Any]]) -> tuple[int, int]:
        index, line = pair
        found = re.search(r"(\d+)$", str(line.get("line_id", "")))
        return (int(found.group(1)) if found else 10**6, index)

    ordered = [line for _, line in sorted(enumerate(lines), key=key)]
    return [{"number": position, **dict(line)} for position, line in enumerate(ordered, start=1)]


def _returnable(line: Mapping[str, Any]) -> bool:
    remaining = line.get("remaining_qty")
    return not (isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining < 1)


def _brief(line: Mapping[str, Any]) -> dict[str, Any]:
    entry = {
        key: line[key]
        for key in ("line_id", "number", "product_name", "color", "category", "size", "unit_paid_cents", "unit_paid_text")
        if line.get(key) is not None
    }
    return entry


def _match_one(phrase: str, lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    text = " ".join(str(phrase).split())[:MAX_PHRASE_CHARS]
    result: dict[str, Any] = {"query": text}
    pool = [line for line in lines if _returnable(line)]

    as_number = _NUMBER.match(text)
    if as_number:
        position = int(as_number.group(1))
        hits = [line for line in pool if line.get("number") == position]
        return _verdict(result, hits)

    tokens = [t for t in _words(text) if t not in STOP_WORDS]
    # The vocabulary is the WHOLE order: a word for a line that is already returned must match nothing, not be
    # dropped as noise (otherwise "the tan boot" would silently become "a boot").
    colours = {_norm(line.get("color", "")) for line in lines} - {""}
    categories = {w for line in lines for w in _words(line.get("category"))}
    names = {w for line in lines for w in _words(line.get("product_name"))}

    colour_tokens = [t for t in tokens if t in colours]
    rest = [t for t in tokens if t not in colour_tokens]
    category_tokens = [t for t in rest if t in categories]
    name_tokens = [t for t in rest if t in names and t not in category_tokens]
    if not (colour_tokens or category_tokens or name_tokens):
        return _verdict(result, [])

    hits = list(pool)
    if colour_tokens:  # colour first ...
        hits = [line for line in hits if _norm(line.get("color", "")) in colour_tokens]
    if category_tokens:  # ... then category (every word of the phrase must be a word of the category) ...
        wanted = set(category_tokens)
        hits = [line for line in hits if wanted <= set(_words(line.get("category")))]
    if name_tokens:  # ... then the product name
        hits = [line for line in hits if _norm(line.get("product_name", "")) in name_tokens]
    return _verdict(result, hits)


def _verdict(result: dict[str, Any], hits: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(hits) == 1:
        return {**result, "status": "matched", **_brief(hits[0])}
    if hits:
        return {**result, "status": "ambiguous", "candidates": [_brief(line) for line in hits]}
    return {**result, "status": "none"}


def resolve(lines: Sequence[Mapping[str, Any]], phrases: Iterable[Any]) -> dict[str, Any]:
    """Match each phrase to a line. ``lines`` carry ``number`` (see ``number_lines``).

    Returns ``{"matches": [...], "selected_line_ids": [...], "complete": bool}``. ``complete`` is True only when
    every phrase matched exactly one line and no line was named twice; otherwise the agent must ask.
    """
    wanted = [p for p in phrases if isinstance(p, str) and p.strip()][:MAX_PHRASES]
    matches: list[dict[str, Any]] = []
    selected: list[str] = []
    for phrase in wanted:
        match = _match_one(phrase, lines)
        if match["status"] == "matched":
            if match["line_id"] in selected:
                match = {**match, "status": "duplicate"}
            else:
                selected.append(match["line_id"])
        matches.append(match)
    complete = bool(matches) and all(m["status"] == "matched" for m in matches)
    return {"matches": matches, "selected_line_ids": selected, "complete": complete}
