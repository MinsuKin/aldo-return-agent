"""The worker's structured logger (Powertools JSON lines: run_id, request id, latency, outcome)."""

from __future__ import annotations

import os

from aws_lambda_powertools import Logger

logger = Logger(service=os.environ.get("POWERTOOLS_SERVICE_NAME", "returns-worker"))
