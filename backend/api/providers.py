"""Late binding for the collaborators the API and the worker call.

The API and the worker are the composition root: they use ``returns_domain`` modules that
other streams write (some of them after this code), and optional packages such as
``personal_agent``. Call sites hold ``lazy("orders")`` proxies, so

* importing the API never fails because a sibling module is not on disk yet,
* a test can replace a whole module with a fake through ``override`` (the provider seam),
* an optional package that is absent degrades to ``None`` instead of crashing every route.

Names without a dot belong to ``returns_domain``; dotted names are used as written
(``personal_agent.memory``, ``api.personal_agent_routes``).
"""

from __future__ import annotations

import importlib
from typing import Any

_overrides: dict[str, Any] = {}


def override(name: str, replacement: Any) -> None:
    """Use ``replacement`` instead of the real module (tests, local experiments)."""
    _overrides[name] = replacement


def clear_overrides() -> None:
    _overrides.clear()


def _qualified(name: str) -> str:
    return name if "." in name else f"returns_domain.{name}"


def module(name: str) -> Any:
    """The module ``name`` (an override wins). A missing module is a real error here."""
    if name in _overrides:
        return _overrides[name]
    return importlib.import_module(_qualified(name))


def optional(name: str) -> Any | None:
    """The module ``name``, or ``None`` when it (or its package) is not installed.

    Only "this module itself is absent" counts as optional. A module that exists but fails
    to import because of a missing dependency raises, so the problem is not hidden.
    """
    if name in _overrides:
        return _overrides[name]
    qualified = _qualified(name)
    try:
        return importlib.import_module(qualified)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if qualified == missing or qualified.startswith(f"{missing}."):
            return None
        raise


class _Lazy:
    """Attribute access on a module that is resolved on every use (so overrides apply)."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        return getattr(module(self._name), attribute)

    def __repr__(self) -> str:
        return f"<lazy {_qualified(self._name)}>"


def lazy(name: str) -> Any:
    """``orders = lazy("orders")`` then ``orders.list_orders(cid)`` anywhere."""
    return _Lazy(name)
