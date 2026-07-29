"""LocalBackend: candidates run in frontend threads, exactly the historical
behavior. The dispatch itself stays in loop._run_candidates (its serial and
ThreadPool paths); this adapter only exists so loop.py can treat both
backends uniformly. Local runs never persist in-flight state, so the journal
is accepted and ignored. The deferred loop import avoids a module cycle
(loop imports execution for the backend factory)."""
from __future__ import annotations

from .base import ExecutionBackend


class LocalBackend(ExecutionBackend):
    def __init__(self, ctx):
        self.ctx = ctx

    def run_candidates(self, *, proposals: list[dict], round_id: int,
                       parent_sha: str, prior_metrics: dict,
                       baseline_metrics: dict, journal=None) -> list[dict]:
        from .. import loop as loop_mod
        return loop_mod._run_candidates(
            self.ctx, proposals, round_id, parent_sha, prior_metrics,
            baseline_metrics)
