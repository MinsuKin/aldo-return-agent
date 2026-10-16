"""Run the Playwright e2e suite (frontend/e2e) against the DEPLOYED site.

    cd backend && ../scripts/with_aws_env.sh env SECRETS_ARN=<arn> uv run --no-sync python ../scripts/test_run_e2e_deployed.py [playwright args]

The admin secret is read from the stack's secret (same path scripts/eval_scenarios.py uses) and handed to Playwright
through the environment of the child process only. Every line the child prints is scrubbed of the secret before it
reaches this process's output, so it is never printed or written. Screenshots go to docs/eval/screenshots/v2/.
Test code only: it changes no product file and deploys nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "backend"))

import eval_scenarios as ev  # noqa: E402

BASE_URL = os.environ.get("DEMO_BASE_URL", "https://d38111d70jquli.cloudfront.net")


def main(argv: list[str]) -> int:
    os.environ.setdefault("TABLE_NAME", ev.ALDO_TABLE)
    os.environ.setdefault("AWS_REGION", ev.REGION)
    ev.ensure_secret_source(ev.REGION)
    from returns_domain import config

    secret = config.get_admin_key()
    secret = secret.decode() if isinstance(secret, bytes) else str(secret)
    env = {
        **os.environ,
        "E2E_BASE_URL": BASE_URL,
        "E2E_TARGET": "deployed",
        "E2E_ADMIN_SECRET": secret,
        "E2E_SHOT_DIR": str(REPO / "docs" / "eval" / "screenshots" / "v2"),
    }
    env.pop("SECRETS_ARN", None)  # the child needs no AWS access
    proc = subprocess.Popen(
        ["npx", "playwright", "test", *argv],
        cwd=REPO / "frontend", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line.replace(secret, "***").rstrip("\n"), flush=True)
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
