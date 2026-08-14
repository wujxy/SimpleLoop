"""One terminal loop round, ready for persistence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from .candidate import (
    CandidateBatchRequest,
    CandidateBatchResult,
    CandidatePlan,
    CandidateResult,
)
from .stages.proposer import ProposalBatch, ProposerRequest
from .stages.selector import Selection, select_candidate


@dataclass(frozen=True)
class SelectionPolicy:
    objective_key: str
    lower_is_better: bool
    require_improvement: bool = True


@dataclass(frozen=True)
class RoundRequest:
    round_id: int
    goal: str
    incumbent_sha: str
    incumbent_metrics: Mapping[str, object]
    selection: SelectionPolicy


class Proposer(Protocol):
    def propose(self, request: ProposerRequest) -> ProposalBatch: ...


class CandidateRunner(Protocol):
    def run(self, request: CandidateBatchRequest) -> CandidateBatchResult: ...


class RoundRecorder(Protocol):
    def record_proposals(
        self,
        request: RoundRequest,
        proposals: ProposalBatch,
    ) -> None: ...


@dataclass(frozen=True)
class RoundResult:
    round_id: int
    parent_sha: str
    proposals: ProposalBatch
    candidates: tuple[CandidateResult, ...]
    selection: Selection
    telemetry: Mapping[str, object] = field(default_factory=dict)

    @property
    def next_sha(self) -> str:
        return self.selection.sha or self.parent_sha


def run_round(
    request: RoundRequest,
    *,
    proposer: Proposer,
    candidates: CandidateRunner,
    recorder: RoundRecorder,
) -> RoundResult:
    proposals = proposer.propose(ProposerRequest(
        request.round_id,
        request.goal,
        request.incumbent_sha,
    ))
    recorder.record_proposals(request, proposals)
    batch = (
        CandidateBatchResult(())
        if proposals.abstained
        else candidates.run(CandidateBatchRequest(
            request.round_id,
            tuple(
                CandidatePlan(index, request.incumbent_sha, proposal)
                for index, proposal in enumerate(proposals.proposals)
            ),
        ))
    )
    incumbent = request.incumbent_metrics.get(request.selection.objective_key)
    selection = select_candidate(
        candidates=batch.candidates,
        objective_key=request.selection.objective_key,
        lower_is_better=request.selection.lower_is_better,
        incumbent_value=(
            float(incumbent)
            if isinstance(incumbent, (int, float))
            and not isinstance(incumbent, bool)
            else None
        ),
        require_improvement=request.selection.require_improvement,
    )
    return RoundResult(
        request.round_id,
        request.incumbent_sha,
        proposals,
        batch.candidates,
        selection,
        batch.telemetry,
    )
