"""ALDO returns agent (Strands on Bedrock AgentCore Runtime).

Import submodules explicitly (``from agent import runner``). This file stays free of
imports on purpose: the AgentCore runtime must start in a few seconds, and the unit tests
import single modules without pulling in the Strands runtime.

Modules: ``context`` (RunContext), ``services`` (late-bound domain modules), ``envelope`` (tool
result envelope and trace emission), ``tools`` (the ten tools), ``hooks`` (round limit),
``ranking`` (deterministic option order), ``prompt``, ``runner`` (one run), ``log``
(structured logging), ``preflight`` (model check).
"""
