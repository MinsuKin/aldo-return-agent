"""Demo plumbing routes: personas, sign-in, reset (admin) and the impact model.

``customer_id`` appears in a body exactly once: ``POST /demo/sessions``, validated against the
fixture personas (the documented exemption to requirement 1.2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from returns_domain import auth, clock, config
from returns_domain.errors import DomainError, ErrorCode

from . import providers
from .http import ADMIN, PUBLIC, Api, Reply, Request, logger
from .models import ResetBody, SessionBody

fixtures = providers.lazy("fixtures")
audit = providers.lazy("audit")
store = providers.lazy("store")

#: <repo>/backend/api/routes_demo.py -> <repo>
_REPO_ROOT = Path(__file__).resolve().parents[2]


def register(api: Api) -> None:
    @api.get("/demo/personas", auth=PUBLIC)
    def personas(req: Request) -> dict[str, Any]:
        return {"personas": fixtures.persona_list()}

    @api.post("/demo/sessions", auth=PUBLIC)
    def create_session(req: Request) -> dict[str, Any]:
        body = req.body_model(SessionBody)
        known = {persona["customer_id"]: persona for persona in fixtures.persona_list()}
        persona = known.get(body.customer_id)
        if persona is None:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Unknown demo persona.")
        token = auth.sign_token(body.customer_id)  # scope "session", 8 hours
        return {
            "token": token.token,
            "expires_at": clock.iso(token.expires_at),
            "customer": {
                "customer_id": persona["customer_id"],
                "display_name": persona.get("display_name"),
                "home_region": _home_region(persona),
            },
        }

    @api.post("/demo/reset", auth=ADMIN)
    def reset(req: Request) -> dict[str, Any]:
        # The admin header was verified before the body is read, so an unauthenticated caller
        # learns nothing about the validation rules.
        body = req.body_model(ResetBody, optional=True)
        summary = fixtures.reset(body.variant)
        _reset_personal_agent_memory()
        audit.write_audit("admin", "reset", "allowed", {"variant": body.variant})
        req.annotate(variant=body.variant)
        return {
            "ok": True,
            "variant": body.variant,
            "deleted": _count(summary, "deleted"),
            "written": _count(summary, "written"),
        }

    @api.get("/impact", auth=PUBLIC)
    def impact(req: Request) -> Reply:
        path = impact_json_path()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise DomainError(ErrorCode.NOT_FOUND, "The impact model is not available.") from None
        json.loads(text)  # a broken document fails loudly (500) instead of reaching the UI
        return Reply(raw_json=text)


def impact_json_path() -> Path:
    """IMPACT_JSON_PATH, else the bundled copy next to the fixtures, else docs/impact/."""
    configured = os.environ.get("IMPACT_JSON_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    bundled = config.get_settings().fixtures_dir / "impact-model.json"
    if bundled.is_file():
        return bundled
    return _REPO_ROOT / "docs" / "impact" / "impact-model.json"


def _home_region(persona: dict[str, Any]) -> str | None:
    """The persona's home region: from the fixture record, else from the customer's item."""
    region = persona.get("home_region")
    if region is None:
        item = store.get_item(store.customer_key(persona["customer_id"]))
        region = (item or {}).get("home_region")
    return region


def _count(summary: Any, name: str) -> int:
    value = summary.get(name) if isinstance(summary, dict) else getattr(summary, name, 0)
    return int(value or 0)


def _reset_personal_agent_memory() -> None:
    """Restore the personal agent's memory when that package is installed."""
    memory = providers.optional("personal_agent.memory")
    if memory is None:
        logger.info("personal_agent.memory not installed; skipped its reset")
        return
    resetter = getattr(memory, "reset_memory", None) or memory.reset
    resetter()
