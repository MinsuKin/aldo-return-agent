"""HTTP API Lambda for the ALDO Fit Gateway prototype (composition root).

``api.handler.handler`` is the Lambda entry point. Route modules live next to it
(``routes_demo``, ``routes_returns``, ``routes_gateway``); ``personal_agent_routes`` belongs to
the personal-agent stream and is only included when it exists.
"""
