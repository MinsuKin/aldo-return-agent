"""Opt-in live run of David's bundle return (real Bedrock). Skipped unless ``ALDO_LIVE_AGENT=1``.

    ALDO_LIVE_AGENT=1 ../scripts/with_aws_env.sh uv run --no-sync pytest tests/agent/test_live_david_dialogue.py -q -s

The driver and its checks live in ``live_david_dialogue.py``; see its docstring.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ALDO_LIVE_AGENT") != "1", reason="live Bedrock run; set ALDO_LIVE_AGENT=1 to enable"
)


def test_david_dialogue_live_second_bundle_override_and_silver() -> None:
    from . import live_david_dialogue

    assert live_david_dialogue.main(["--runs", "1", "--second", "--override", "--silver"]) == 0
