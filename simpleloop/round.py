"""One terminal loop round, ready for persistence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .candidate import CandidateResult
from .stages.proposer import ProposalBatch
from .stages.selector import Selection


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
