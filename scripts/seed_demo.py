#!/usr/bin/env python3
"""Seed (reset) the demo table with the synthetic fixtures.

Deletes every runtime partition (conversations, runs, quotes, transactions, cases, audit, size
requests, write-back outbox, agent-placed orders) and writes the fixture items for a variant.
Prints counts only. Credentials come from the environment, so run it through the helper:

    /Users/minsu/Desktop/aldo-return-agent/scripts/with_aws_env.sh \
        uv run --project /Users/minsu/Desktop/aldo-return-agent/backend \
        python /Users/minsu/Desktop/aldo-return-agent/scripts/seed_demo.py --variant default

Local moto or DynamoDB Local: set ``AWS_ENDPOINT_URL_DYNAMODB`` and pass ``--create-table``.
The table name comes from ``TABLE_NAME`` (default ``aldo-returns-demo``). Fixtures come from
``FIXTURES_DIR`` (default: ``backend/fixtures_bundle`` if present, else ``fixtures/``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from returns_domain import config, fixtures, store  # noqa: E402
from returns_domain.errors import DomainError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--variant", choices=fixtures.VARIANTS, default="default")
    parser.add_argument(
        "--create-table",
        action="store_true",
        help="create the table first (local moto or DynamoDB Local; an existing table is left alone)",
    )
    args = parser.parse_args(argv)

    settings = config.get_settings()
    try:
        if args.create_table:
            store.create_table()
        summary = fixtures.reset(args.variant)
    except DomainError as exc:
        print(f"seed failed: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    print(f"table:   {settings.table_name} ({settings.region})")
    print(f"variant: {summary.variant}")
    print(f"deleted: {summary.deleted} runtime items")
    print(f"written: {summary.written} fixture items")
    for name, count in summary.counts.items():
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
