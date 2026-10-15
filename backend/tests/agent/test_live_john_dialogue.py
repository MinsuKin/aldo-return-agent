"""Opt-in live run of John's return dialogue (real Bedrock). Skipped unless ``ALDO_LIVE_AGENT=1``.

    ALDO_LIVE_AGENT=1 ../scripts/with_aws_env.sh uv run --no-sync pytest tests/agent/test_live_john_dialogue.py -q -s

The driver and its checks live in ``live_john_dialogue.py``; see its docstring.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ALDO_LIVE_AGENT") != "1", reason="live Bedrock run; set ALDO_LIVE_AGENT=1 to enable"
)


def test_john_dialogue_live_with_and_without_memory_and_adversarial() -> None:
    from . import live_john_dialogue

    assert live_john_dialogue.main(["--runs", "1", "--adversarial"]) == 0
