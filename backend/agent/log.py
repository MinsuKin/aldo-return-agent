"""Structured JSON logging for the agent (G-30): run_id, tool, latency_ms, outcome.

Powertools Logger prints one JSON object per line to stdout, which AgentCore ships to
CloudWatch Logs. Nothing here logs customer messages, tokens or secrets: call sites pass
ids, tool names, codes and timings only.
"""

from __future__ import annotations

import os

from aws_lambda_powertools import Logger

logger = Logger(
    service=os.environ.get("POWERTOOLS_SERVICE_NAME", "aldo-returns-agent"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
