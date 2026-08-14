"""Execution backends: how one round's candidates get executed.

LocalBackend runs candidates in frontend threads (the historical behavior);
HEPJobBackend submits each candidate as a condor job running the standalone
CandidateWorker. A backend returns only BUSINESS-terminal candidate dicts —
infrastructure failures are retried/accounted for inside the backend and
never reach the proposer's history as proposal failures.

A backend's only job is "proposals in -> candidate results out". It does NOT
own round-level harness state:

  - proposals are plain strings so the execution layer never imports the
    Researcher role;
  - in-flight persistence goes through a RoundJournal supplied by the loop,
    which owns the file, the round meta, and the clear-on-success decision.
"""
from __future__ import annotations

from ..stages.proposer import ProposalBatch, ProposerRequest


class InfraRoundError(RuntimeError):
    """Every candidate of a round died of infrastructure causes; the round
    must not be recorded and the proposer must not see these failures."""


class RoundJournal:
    """Loop-owned persistence of one in-flight round.

    The backend calls save(jobs) on every job-state transition so a frontend
    crash can be resumed; the loop owns the file location, the round meta
    (proposer outputs, parent chain), and clear() on success. The backend
    never sees the meta. save() receives the backend's opaque job table —
    the same list resume_round() later hands back to the backend.
    """

    def save(self, jobs: list[dict]) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class ExecutionBackend:
    def run_candidates(self, *, proposals: list[str], round_id: int,
                       parent_sha: str,
                       journal: "RoundJournal | None" = None,
                       ) -> list[dict]:
        raise NotImplementedError

    def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
        """Run the baseline evaluation for the unoptimized baseline commit.

        Returns (eval_block, metrics). A failure should raise BaselineAcceptanceError.
        """
        raise NotImplementedError

    def resume_round(self, jobs: list[dict], *, round_id: int,
                     parent_sha: str,
                     journal: "RoundJournal | None" = None,
                     ) -> list[dict]:
        """Re-enter the poll loop for an in-flight round after a frontend
        restart. `jobs` is the opaque job table the backend previously passed
        to journal.save(); the proposer is NOT called again. The default
        backend (local) never persists in-flight state, so a leftover
        inflight file implies a misconfigured resume."""
        raise NotImplementedError

    def run_proposer_lanes(self, request: ProposerRequest) -> ProposalBatch:
        """Run one round's proposer lanes — each lane researches in its own
        writable workspace — and return a Host ProposalBatch. The local backend
        runs lanes as frontend threads; HEPJobBackend submits one condor job
        per lane (simpleloop.proposer_lane_worker). The returned proposals fan
        out to run_candidates as usual."""
        raise NotImplementedError

    def run_self_review(self, *, round_id: int) -> dict:
        """Run one RSI self-review round (RSI S3c.2): spawn the proposer-lane
        worker in self mode and return its ``self_review`` payload dict
        (decision / diagnosis / keep_reason / next_review_after_rounds /
        self_change / incumbent_self_sha / abstained). The Host appends it to
        reviews.jsonl and writes the commitment back; the worker resolves the
        incumbent self-repo itself, so there is no base_sha argument. v0
        implements Local only; HEPJob defers to S3c.3."""
        raise NotImplementedError

    def cleanup_proposer_orphans(self) -> None:
        """Kill any proposer-lane jobs a crashed frontend left running and
        clear the inflight_proposer marker. Called by the loop at round start
        so an interrupted proposer run is re-proposed cleanly instead of
        leaking orphan jobs. The default is a no-op (local lanes are frontend
        threads that die with the process)."""
        return None
