"""Import boundary (PIVOT_ADDENDUM 4, contracts.md F.0, F.12), checked on the syntax trees.

* ``personal_agent`` never imports ``returns_domain.store`` (or the modules that use it). At module
  level it may import only ``config, errors, clock, money, auth``; inside functions it may also
  lazy-import ``gateway`` (the ``local`` transport, gateway_client.py only) and ``trace``
  (``emit_a2a``, a2a.py only).
* ``returns_domain`` never imports ``personal_agent``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
PERSONAL_AGENT = BACKEND / "personal_agent"
RETURNS_DOMAIN = BACKEND / "returns_domain"

TOP_LEVEL_ALLOWED = {"config", "errors", "clock", "money", "auth"}
LAZY_ALLOWED = {"gateway_client.py": {"gateway"}, "a2a.py": {"trace"}}
STORE_USERS = {
    "store", "fixtures", "orders", "options", "quotes", "execution", "cases", "audit",
    "conversations", "runs", "memory_updates", "personal_agent_client", "pa_client",
}


def returns_domain_imports(path: Path) -> list[tuple[str, bool, int]]:
    """(module name below returns_domain, is_lazy, line) for every returns_domain import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, bool, int]] = []

    def visit(node: ast.AST, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            inside = lazy or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(child, ast.ImportFrom) and child.module:
                parts = child.module.split(".")
                if parts[0] == "returns_domain":
                    if len(parts) > 1:
                        found.append((parts[1], lazy, child.lineno))
                    else:
                        found.extend((alias.name, lazy, child.lineno) for alias in child.names)
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    parts = alias.name.split(".")
                    if parts[0] == "returns_domain" and len(parts) > 1:
                        found.append((parts[1], lazy, child.lineno))
            visit(child, inside)

    visit(tree, False)
    return found


def package_files() -> list[Path]:
    return sorted(PERSONAL_AGENT.glob("*.py")) + [BACKEND / "api" / "personal_agent_routes.py"]


@pytest.mark.parametrize("path", sorted(PERSONAL_AGENT.glob("*.py")), ids=lambda p: p.name)
def test_personal_agent_modules_import_only_the_allowed_domain_modules(path: Path) -> None:
    lazy_ok = LAZY_ALLOWED.get(path.name, set())
    for module, lazy, line in returns_domain_imports(path):
        assert module not in STORE_USERS, f"{path.name}:{line} imports returns_domain.{module}"
        allowed = TOP_LEVEL_ALLOWED | (lazy_ok if lazy else set())
        assert module in allowed, f"{path.name}:{line} imports returns_domain.{module} ({'lazily' if lazy else 'at module level'})"


def test_the_store_is_never_imported_anywhere_in_the_package_or_its_routes() -> None:
    for path in package_files():
        modules = {module for module, _lazy, _line in returns_domain_imports(path)}
        assert "store" not in modules, path.name


def test_the_routes_use_only_auth_and_errors_from_the_domain() -> None:
    modules = {m for m, _l, _n in returns_domain_imports(BACKEND / "api" / "personal_agent_routes.py")}
    assert modules <= {"auth", "errors"}


def test_returns_domain_never_imports_the_personal_agent() -> None:
    offenders = []
    for path in sorted(RETURNS_DOMAIN.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name == "personal_agent" or name.startswith("personal_agent.") for name in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_the_scan_sees_the_known_lazy_imports() -> None:
    gateway = {(m, lazy) for m, lazy, _n in returns_domain_imports(PERSONAL_AGENT / "gateway_client.py")}
    assert ("gateway", True) in gateway and ("auth", False) in gateway
    a2a = {(m, lazy) for m, lazy, _n in returns_domain_imports(PERSONAL_AGENT / "a2a.py")}
    assert ("trace", True) in a2a
