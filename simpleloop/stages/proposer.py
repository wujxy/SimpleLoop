"""Small Host-facing contract for the separately packaged proposer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProposerRequest:
    round_id: int
    goal: str
    incumbent_sha: str


@dataclass(frozen=True)
class Proposal:
    instruction: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise ValueError("proposal instruction must not be empty")


@dataclass(frozen=True)
class Abstention:
    reason: str
    blocking_unknown: str | None = None


@dataclass(frozen=True)
class ProposalBatch:
    proposals: tuple[Proposal, ...]
    abstention: Abstention | None = None
    telemetry: Mapping[str, object] = field(default_factory=dict)
    trace: Mapping[str, object] = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        return not self.proposals


class StaticProposer:
    """Expose a fixed experiment list through the normal proposer port."""

    def __init__(self, instructions: Sequence[str]):
        self.instructions = tuple(instructions)

    def propose(self, request: ProposerRequest) -> ProposalBatch:
        return ProposalBatch((Proposal(self.instructions[request.round_id]),))


def decode_lane_proposals(
    rows: Sequence[Mapping[str, object]],
) -> tuple[Proposal, ...]:
    """Decode only proposal facts the Host executes or persists."""
    return tuple(
        Proposal(
            instruction=str(row["instruction"]),
            evidence_refs=tuple(
                str(ref) for ref in row.get("evidence_refs") or ()
            ),
        )
        for row in rows
    )
