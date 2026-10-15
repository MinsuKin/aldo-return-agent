"""ALDO Fit Gateway: the owner's personal shopping agent (prototype, synthetic data).

Import submodules explicitly (``from personal_agent import memory``). This file stays free of
imports on purpose so that importing one submodule never pulls in boto3 clients or Strands.

Boundary (PIVOT_ADDENDUM section 4): this package never imports ``returns_domain.store``. It may
import ``returns_domain.{config,errors,clock,money,auth}``; the ``local`` gateway transport lazily
imports ``returns_domain.gateway``; ``a2a.py`` lazily imports ``returns_domain.trace.emit_a2a``.
``returns_domain`` never imports this package.
"""
