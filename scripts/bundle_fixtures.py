#!/usr/bin/env python3
"""Copy fixtures/ and docs/impact/impact-model.json into backend/fixtures_bundle/ (gitignored).

Thin wrapper: the work is done by scripts/bundle_backend.sh (run it directly for --lambda).
Run this before `agentcore deploy` and before a real-code CDK deploy.
"""
import subprocess
import sys
from pathlib import Path

script = Path(__file__).resolve().parent / "bundle_backend.sh"
sys.exit(subprocess.call([str(script), *sys.argv[1:]]))
