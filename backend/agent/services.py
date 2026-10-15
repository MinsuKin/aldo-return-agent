"""Late-bound access to the persistence-layer modules of ``returns_domain``.

The tools and the runner never import ``returns_domain.orders`` (and friends) at module
import time. They read them from a ``Services`` handle, which

* keeps agent start-up light (the AgentCore runtime has a 30 s initialisation budget),
* lets the unit tests inject fakes (``Services(orders=FakeOrders(), ...)``) and run before
  or without the real modules, and
* makes the optional ``gateway`` module (PIVOT_ADDENDUM 9.3, written by another stream)
  importable on demand: a missing module raises ``ImportError`` only when it is used.

Only the names below are served. ``Services`` never becomes a general import hook.
"""

from __future__ import annotations

import importlib
from typing import Any

SERVICE_NAMES: frozenset[str] = frozenset(
    {
        "store",
        "orders",
        "options",
        "quotes",
        "execution",
        "cases",
        "trace",
        "audit",
        "conversations",
        "runs",
        "gateway",
    }
)


class Services:
    """``services.orders`` is ``returns_domain.orders`` unless an override was given."""

    def __init__(self, **overrides: Any) -> None:
        unknown = set(overrides) - SERVICE_NAMES
        if unknown:
            raise ValueError(f"unknown service name(s): {', '.join(sorted(unknown))}")
        self._modules: dict[str, Any] = dict(overrides)

    def __getattr__(self, name: str) -> Any:
        # Only called when normal lookup fails; private names must not recurse into _modules.
        if name.startswith("_") or name not in SERVICE_NAMES:
            raise AttributeError(name)
        modules = self.__dict__.setdefault("_modules", {})
        module = modules.get(name)
        if module is None:
            module = importlib.import_module(f"returns_domain.{name}")
            modules[name] = module
        return module

    def override(self, **overrides: Any) -> "Services":
        """A copy with some services replaced (used by tests)."""
        merged = dict(self._modules)
        merged.update(overrides)
        return Services(**merged)

    def available(self, name: str) -> bool:
        """True if ``name`` can be served (override present or the module imports)."""
        try:
            getattr(self, name)
        except (ImportError, AttributeError):
            return False
        return True
