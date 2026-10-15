"""The outbound text sanitizer (PIVOT_ADDENDUM C3): retailer labels, other purchases, numbers, length."""

from __future__ import annotations

import pytest

from personal_agent.sanitize import (
    PrivateTerms,
    contains_private_term,
    count_words,
    limit_words,
    normalize_text,
    sanitize_public_text,
    strip_markdown,
    truncate,
)

AVA_VIEW = {
    "typical_size": "7.5",
    "memory": [
        {"retailer": "Retailer A (synthetic)", "item": "running shoe", "size": "7.5", "outcome": "kept"},
        {"retailer": "Retailer B (synthetic)", "item": "loafer", "size": "7.5", "outcome": "kept"},
        {"retailer": "Retailer C (synthetic)", "item": "sneaker", "size": "8.0", "outcome": "returned_too_big"},
    ],
}


@pytest.fixture
def terms() -> PrivateTerms:
    return PrivateTerms.from_memory(AVA_VIEW)


def clean(text: str, terms: PrivateTerms, limit: int = 120) -> str:
    return sanitize_public_text(text, terms, max_chars=limit)


def test_removes_every_retailer_label_in_the_owners_memory(terms: PrivateTerms) -> None:
    text = "I kept shoes from Retailer A (synthetic), Retailer B and retailer c (SYNTHETIC) last year"
    result = clean(text, terms)
    assert "retailer" not in result.lower()
    assert "synthetic" not in result.lower()


def test_removes_a_generic_retailer_label_even_if_memory_does_not_have_it(terms: PrivateTerms) -> None:
    assert "retailer" not in clean("Retailer Z had it in 9", terms).lower()


def test_removes_numbers_that_come_from_other_purchases(terms: PrivateTerms) -> None:
    result = clean("Style runs small; my usual 7.5 and a past 8.0 pair suggest 8,0 or 8", terms)
    assert "7.5" not in result and "8.0" not in result and "8,0" not in result
    assert not any(token in result.split() for token in ("8", "7.5"))


def test_keeps_numbers_that_are_aldos_own_signal(terms: PrivateTerms) -> None:
    result = clean("ALDO says 80% of 582 fit returns were too small", terms)
    assert "80%" in result and "582" in result


def test_never_strips_the_word_aldo(terms: PrivateTerms) -> None:
    result = clean("ALDO bot used ALDO's fit signal", terms)
    assert "ALDO bot" in result and "ALDO's" in result


def test_aldo_survives_even_if_a_label_were_set_to_it() -> None:
    odd = PrivateTerms.from_memory(
        {"typical_size": "7.5", "memory": [{"retailer": "ALDO", "item": "aldo", "size": "7.5"}]}
    )
    assert "ALDO" in sanitize_public_text("ALDO fit signal", odd, max_chars=120)


def test_removes_the_item_names_of_other_purchases(terms: PrivateTerms) -> None:
    result = clean("Like my sneaker and my running shoe but this style runs small", terms)
    assert "sneaker" not in result and "running shoe" not in result
    assert "runs small" in result


def test_truncates_to_the_limit_on_a_word_boundary(terms: PrivateTerms) -> None:
    result = clean("fit " * 100, terms)
    assert len(result) <= 120
    assert not result.endswith(" ")
    assert len(clean("x" * 500, terms, 120)) == 120


def test_a_label_cannot_hide_behind_invisible_characters_or_width_tricks(terms: PrivateTerms) -> None:
    sneaky = "Re\u200btailer\u00a0C (synthetic) and ＲＥＴＡＩＬＥＲ　A"
    result = clean(sneaky, terms)
    assert "retailer" not in result.lower()


def test_leaves_no_empty_brackets_or_orphan_punctuation(terms: PrivateTerms) -> None:
    result = clean("Runs small (per Retailer A, 7.5), half size up , ; ,", terms)
    assert "()" not in result and ", ," not in result
    assert not result.endswith((",", ";", ":", "-"))


def test_is_idempotent(terms: PrivateTerms) -> None:
    once = clean("Retailer C said 8.0 was too big; ALDO signal runs small", terms)
    assert clean(once, terms) == once


def test_nothing_private_survives_a_hostile_rationale(terms: PrivateTerms) -> None:
    hostile = (
        "My Retailer A (synthetic) running shoe in 7.5 was kept, Retailer B loafer 7.5 kept, "
        "Retailer C sneaker 8.0 returned too big. ALDO says it runs small."
    )
    result = clean(hostile, terms)
    assert not contains_private_term(result, terms)
    assert "ALDO says it runs small" in result


def test_contains_private_term_detects_leaks(terms: PrivateTerms) -> None:
    assert contains_private_term("bought at Retailer C", terms)
    assert contains_private_term("my usual is 7.5", terms)
    assert not contains_private_term("ALDO says 80% runs small", terms)


def test_empty_memory_still_strips_generic_labels_and_truncates() -> None:
    empty = PrivateTerms()
    assert sanitize_public_text("Retailer Q rocks", empty, max_chars=120) == "rocks"
    assert sanitize_public_text(None, empty, max_chars=120) == ""


def test_normalize_text_collapses_whitespace_and_controls() -> None:
    assert normalize_text("a\n\tb\x00  c\u200b") == "a b c"


def test_truncate_and_word_limits() -> None:
    assert truncate("abc", 5) == "abc"
    assert len(truncate("word " * 50, 20)) <= 20
    words = " ".join(["w"] * 120)
    limited = limit_words(words, 100)
    assert count_words(limited.replace("…", "")) == 100 and limited.endswith("…")
    assert limit_words("short reply", 100) == "short reply"


def test_word_limit_prefers_ending_on_a_complete_sentence() -> None:
    text = (
        "One two three four five six seven. Eight nine ten eleven twelve thirteen fourteen. "
        "Fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo."
    )
    limited = limit_words(text, 18)  # the cut falls inside the third sentence
    assert limited.endswith("fourteen.") and count_words(limited) == 14 and "…" not in limited
    junk = "First sentence is complete. " + "tail " * 50  # a stop too early to keep: ellipsis instead
    assert limit_words(junk, 20).endswith("…")
    exact = "One two three. " * 10  # the cut lands exactly on a full stop
    assert limit_words(exact, 9).endswith("three.")
    assert limit_words("Keeps\nits\nline breaks when short", 100) == "Keeps\nits\nline breaks when short"


def test_strip_markdown_leaves_plain_sentences() -> None:
    text = "- **Size:** 8.5.\n- **Pickup:** ALDO Demo Store Montreal.\n## Why\n1. `runs_small` per ALDO."
    assert strip_markdown(text) == "Size: 8.5.\nPickup: ALDO Demo Store Montreal.\nWhy\nruns_small per ALDO."
    assert strip_markdown("Plain text with 2026-10-17 and runs_small stays.") == "Plain text with 2026-10-17 and runs_small stays."
