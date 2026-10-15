"""AgentCore Runtime entrypoint of the ALDO returns agent (contracts B.5, spike.md 0.2 and 3.3).

* Local: ``python agent_app.py`` serves ``POST http://localhost:8080/invocations`` and
  ``GET /ping`` (``AGENT_MODE=local`` in the worker points at it).
* Deployed: ``agentcore configure -e agent_app.py ... -dt direct_code_deploy`` then
  ``agentcore deploy`` (the toolkit packages this directory; ``fixtures_bundle/`` must be
  built first by ``scripts/bundle_fixtures.py``).

The payload comes only from the backend worker (the runtime is invocable by the worker's IAM
role only), so ``customer_id`` in it is trusted. The second parameter must be called
``context`` for the SDK to pass the request context; the agent does not need it.

Import-time work stays light: AgentCore gives the runtime 30 s to initialise. No client is
opened and no data is loaded here; the runner builds everything per request.
"""

from __future__ import annotations

from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent import runner

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Run one agent turn. Returns ``{"run_id", "status", "metrics"}`` (contracts C.7)."""
    return runner.run(payload)


if __name__ == "__main__":
    app.run()
