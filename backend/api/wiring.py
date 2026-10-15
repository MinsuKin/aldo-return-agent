"""Composition-root helpers shared by the API Lambda and the worker Lambda.

* ``register_local_write_back``: with ``GATEWAY_TRANSPORT=local`` (tests, local runs) ALDO's
  write-back client calls the personal agent in process. ``returns_domain`` never imports
  ``personal_agent``, so this composition root hands it the handler.
* ``flush_outbox_quietly``: push queued size write-backs to the personal agent. A failed push
  is retried later (the outbox keeps the item), so nothing here may ever raise.
"""

from __future__ import annotations

import os
from typing import Any

from aws_lambda_powertools import Logger

from returns_domain import config

from . import providers

logger = Logger(service=os.environ.get("POWERTOOLS_SERVICE_NAME", "returns-api"))


def _write_back_handler(memory: Any) -> Any:
    """The personal agent's idempotent write-back entry point (current name, then the old one)."""
    return getattr(memory, "apply_memory_update", None) or getattr(memory, "apply_update", None)


def register_local_write_back() -> bool:
    """Wire the in-process write-back handler when the transport is ``local``."""
    if config.get_settings().gateway_transport != "local":
        return False
    client = providers.optional("personal_agent_client")
    memory = providers.optional("personal_agent.memory")
    handler = _write_back_handler(memory) if memory is not None else None
    register = getattr(client, "register_local_handler", None) if client is not None else None
    if handler is None or register is None:
        logger.warning("local write-back handler not registered (personal agent or client missing)")
        return False
    register(handler)
    return True


def flush_outbox_quietly(customer_id: str | None = None, *, limit: int = 10) -> int:
    """``gateway.flush_outbox``: the number of write-backs delivered. Never raises."""
    try:
        gateway = providers.optional("gateway")
        if gateway is None:
            return 0
        return int(gateway.flush_outbox(customer_id, limit=limit) or 0)
    except Exception:
        logger.exception("flush_outbox_failed")
        return 0
