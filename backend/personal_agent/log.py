"""One structured logger for the package (JSON lines via Powertools, G-30).

Log fields go in ``extra={...}`` (run_id, tool, latency_ms, outcome, ...). Never log tokens,
Authorization headers, secrets or the owner's memory content.
"""

from __future__ import annotations

from aws_lambda_powertools import Logger

logger = Logger(service="aldo-personal-agent")
