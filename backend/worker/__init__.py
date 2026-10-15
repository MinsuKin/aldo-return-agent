"""Worker: starts and finishes one agent run per asynchronous invocation.

``worker.enqueue_async`` is the API side of the hand-off (it invokes the worker Lambda with
``InvocationType="Event"``, or runs the worker inline in a thread when ``AGENT_MODE=local``).
``worker.handler.handler`` is the Lambda entry point. The worker Lambda is never retried by
Lambda itself (design-gap G-05); the conditional ``queued -> running`` claim makes any extra
invocation harmless.
"""

from .enqueue import enqueue_async

__all__ = ["enqueue_async"]
