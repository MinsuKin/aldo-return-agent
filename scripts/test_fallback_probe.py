"""Prove the FALLBACK Bedrock model path end to end on the real demo dialogues.

Why a wrapper: the live dialogue scripts call ``configure_environment()``, which POPS
BEDROCK_MODEL_ID / BEDROCK_FALLBACK_MODEL_ID so a run always uses the config defaults
(Sonnet primary, Opus fallback). To exercise the fallback we must set the env vars AFTER
that pop and BEFORE each run. The runner reads ``config.get_settings()`` live on every
turn, so the primary (a non-existent model id) fails on the FIRST model call before any
tool call, and ``agent/runner.py`` retries once on the fallback (``fallback_used=True``).

This drives the SAME turns and the SAME acceptance checks as live_david_dialogue.py
(and optionally John via its silver/second beats), against in-process moto tables. Real
Bedrock calls go out ONLY to the fallback model. Nothing is written to AWS; the deployed
tables are never touched.

Run from /Users/minsu/Desktop/aldo-return-agent/backend (creds only via the helper):

    ../scripts/with_aws_env.sh uv run --no-sync python ../scripts/test_fallback_probe.py --david --second
    ../scripts/with_aws_env.sh uv run --no-sync python ../scripts/test_fallback_probe.py --david --second --john-silver

Flags:
  --david        David's bundle dialogue (turns 1-3, fee 0 + perk, execution done)   [1 run]
  --second       add David's second-bundle beat (standard CAD 5.95, exchange executed) [+turns]
  --john-silver  John's Silver bundle (needs_review -> case, no quote)                [1 run]

BAD_PRIMARY is a syntactically valid but non-existent inference-profile id, so the first
model call raises before any tool runs. Exit 0 only if every check passed AND every turn
reported fallback_used=True. At most a handful of real fallback calls per run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from moto import mock_aws  # noqa: E402

from tests.agent import live_david_dialogue as D  # noqa: E402

BAD_PRIMARY = "us.anthropic.claude-does-not-exist-5-5"
FALLBACK = "us.anthropic.claude-opus-5-5"


def force_fallback_env() -> None:
    """Set AFTER configure_environment() so the pop cannot undo it."""
    os.environ["BEDROCK_MODEL_ID"] = BAD_PRIMARY
    os.environ["BEDROCK_FALLBACK_MODEL_ID"] = FALLBACK
    # The personal_agent_model_id defaults to BEDROCK_MODEL_ID; irrelevant for the returns flow
    # but set so nothing accidentally uses the bad id as a working primary.
    os.environ.pop("PERSONAL_AGENT_MODEL_ID", None)


def assert_fallback(result: D.Result) -> None:
    """Add two cross-cutting checks: the primary id was the bad one and every turn fell back."""
    for turn in result.turns:
        m = turn.metrics
        result.check(
            f"{turn.name}: fallback_used=True",
            bool(m.get("fallback_used")) is True,
            f"fallback_used={m.get('fallback_used')} model_used={m.get('model_id_used')}",
        )
        result.check(
            f"{turn.name}: model_id_used is the fallback (opus-5-5)",
            m.get("model_id_used") == FALLBACK,
            f"model_id_used={m.get('model_id_used')}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Force the Bedrock fallback and run the demo dialogues")
    parser.add_argument("--david", action="store_true", help="David's bundle dialogue (1 run)")
    parser.add_argument("--second", action="store_true", help="add David's second-bundle beat")
    parser.add_argument("--john-silver", action="store_true", help="John's Silver bundle (needs_review)")
    args = parser.parse_args(argv)

    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        print("AWS credentials are not in the environment; run through scripts/with_aws_env.sh", file=sys.stderr)
        return 2
    if not (args.david or args.john_silver):
        args.david = True  # a sensible default: at least David

    D.configure_environment()
    force_fallback_env()
    print(f"[probe] primary (forced to fail) = {BAD_PRIMARY}")
    print(f"[probe] fallback (under test)     = {FALLBACK}")

    results: list[D.Result] = []
    with mock_aws(config=D.moto_config()):
        if args.david:
            run_fn = D.play_second if args.second else D.play_dialogue
            result = run_fn(1) if args.second else D.play_dialogue("plain", 1)
            assert_fallback(result)
            D.show(result)
            results.append(result)
        if args.john_silver:
            result = D.play_silver(1)
            assert_fallback(result)
            D.show(result)
            results.append(result)

    D.summarize(results)
    all_ok = all(r.passed for r in results)
    print(f"\n[probe] VERDICT: {'ALL CHECKS PASSED (fallback path works)' if all_ok else 'SOME CHECKS FAILED'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
