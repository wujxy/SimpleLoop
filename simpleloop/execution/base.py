"""Execution backends: how one round's candidates get executed.

LocalBackend runs candidates in frontend threads (the historical behavior);
HEPJobBackend submits each candidate as a condor job running the standalone
CandidateWorker. A backend returns only BUSINESS-terminal candidate dicts —
infrastructure failures are retried/accounted for inside the backend and
never reach the proposer's history as proposal failures.
"""
from __future__ import annotations


class ExecutionBackend:
    def run_candidates(self, *, proposals, round_id: int, parent_sha: str,
                       prior_metrics: dict, reflection: str = "",
                       insight: tuple | None = None) -> list[dict]:
        raise NotImplementedError

    def resume_round(self, inflight: dict) -> list[dict]:
        """Re-enter the poll loop for an in-flight round after a frontend
        restart. The default backend (local) never persists in-flight state,
        so a leftover inflight_round.json implies a misconfigured resume."""
        raise NotImplementedError
