"""Lambda entry point of the HTTP API: ``api.handler.handler`` (the composition root).

``build_app`` registers the route modules and includes the personal-agent router when that
module exists. CloudFront forwards ``/api/*`` unchanged, so the resolver strips the ``/api``
prefix and routes are written without it (design-gap G-10). Powertools validation is off: bodies
are validated by hand so every error keeps the ``{"error": {...}}`` shape.
"""

from __future__ import annotations

from typing import Any, Callable

from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response

from returns_domain.errors import ErrorCode
from worker import enqueue_async

from . import providers, routes_demo, routes_gateway, routes_returns, wiring
from .http import Api, logger, observe, simple_error

#: Owned by the personal-agent stream; included only when it exists.
PERSONAL_AGENT_ROUTES = "api.personal_agent_routes"


def build_app(*, enqueue: Callable[[dict], None] | None = None) -> Api:
    """A fresh app. ``enqueue`` replaces the worker hand-off (tests, local runs)."""
    resolver = APIGatewayHttpResolver(strip_prefixes=["/api"], enable_validation=False)
    resolver.use(middlewares=[observe])
    api = Api(resolver, enqueue=enqueue or enqueue_async)

    @resolver.not_found
    def not_found(_: Exception) -> Response:
        return simple_error(
            404, ErrorCode.NOT_FOUND.value, "Not found.", resolver.context.get("_request_id", "")
        )

    routes_demo.register(api)
    routes_returns.register(api)
    routes_gateway.register(api)
    wiring.register_local_write_back()
    include_personal_agent_routes(api)
    return api


def include_personal_agent_routes(api: Api) -> bool:
    """Include the personal agent's router. The rest of the API works without it."""
    try:
        routes = providers.optional(PERSONAL_AGENT_ROUTES)
        if routes is None:
            logger.info("personal_agent_routes_absent")
            return False
        router = routes.build_router(
            # Late-bound, so swapping ``api.enqueue`` later also reaches the router.
            enqueue=lambda payload: api.enqueue(payload),
            # The owner is about to read their memory: deliver any waiting write-backs first.
            before_memory_read=lambda customer_id: wiring.flush_outbox_quietly(customer_id),
        )
        api.resolver.include_router(router)
    except Exception:
        logger.exception("personal_agent_routes_disabled")
        return False
    return True


app: Api = build_app()


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point (``api.handler.handler``)."""
    return app.resolver.resolve(event, context)


lambda_handler = handler
