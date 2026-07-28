"""Backend factory. `execution.backend` in the resolved config picks the
implementation: "local" (default) or "hepjob"."""
from __future__ import annotations

from .base import ExecutionBackend
from .local import LocalBackend


def build_backend(ctx) -> ExecutionBackend:
    name = (ctx.cfg.get("execution_backend") or "local").strip().lower()
    if name == "local":
        return LocalBackend(ctx)
    if name == "hepjob":
        from .hepjob import HEPJobBackend
        return HEPJobBackend(ctx, ctx.cfg.get("hepjob") or {})
    raise ValueError(
        f"execution.backend: unknown backend {name!r} (expected local|hepjob)")
