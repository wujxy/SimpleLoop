"""Data models for scientific memory.

A Finding is a research question container, not a summary. It never carries
LLM-authored conclusions (no `summary` / `recommendation` / `validated`
fields): those would become an unverified language layer that competes with
the immutable Experiment Ledger for authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


# Operational states — the only lifecycle the current version tracks.
# Epistemic claims (supported/contradicted) are deliberately not modeled;
# see design doc §4.3.
FINDING_STATES = frozenset({"open", "active", "dormant", "archived"})


@dataclass(frozen=True)
class ExistingFindingTarget:
    """The proposal continues an existing research question."""

    finding_id: str


@dataclass(frozen=True)
class NewFindingTarget:
    """The proposal opens a new research question. Mechanisms and code
    regions are optional structured tags used by retrieval and frontier
    coverage; question is the only required field."""

    question: str
    mechanisms: tuple[str, ...] = ()
    code_regions: tuple[str, ...] = ()


ResearchTarget = Union[ExistingFindingTarget, NewFindingTarget]


@dataclass(frozen=True)
class ResearchProposal:
    """One structured proposal: an executor instruction plus the research
    target that gives it scientific meaning.

    ``evidence_refs`` and ``material_difference`` are round-local justification
    surfaced by the Proposer's deliberation: pointers to evidence actually
    examined this round, and (when the proposal resembles a prior one) what
    makes it materially different. They never become Ledger facts; the Loop
    records them only in the non-authoritative proposer trace.
    """

    instruction: str
    research_target: ResearchTarget
    evidence_refs: tuple[str, ...] = ()
    material_difference: str | None = None


@dataclass(frozen=True)
class Finding:
    """A research question and the experiments attached to it.

    Fields are documented in design doc §4.2. Note that no field carries an
    LLM-authored conclusion; stats are derived from the Experiment Ledger.
    """

    id: str
    question: str
    mechanisms: tuple[str, ...]
    code_regions: tuple[str, ...]
    state: str
    created_round: int
    last_touched_round: int
    experiment_refs: tuple[str, ...] = ()
    parent_finding_id: str | None = None
    stats: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in FINDING_STATES:
            raise ValueError(
                f"finding state {self.state!r} not in {sorted(FINDING_STATES)}"
            )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "mechanisms": list(self.mechanisms),
            "code_regions": list(self.code_regions),
            "state": self.state,
            "created_round": self.created_round,
            "last_touched_round": self.last_touched_round,
            "experiment_refs": list(self.experiment_refs),
            "parent_finding_id": self.parent_finding_id,
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        return cls(
            id=str(data["id"]),
            question=str(data["question"]),
            mechanisms=tuple(data.get("mechanisms") or ()),
            code_regions=tuple(data.get("code_regions") or ()),
            state=str(data["state"]),
            created_round=int(data["created_round"]),
            last_touched_round=int(data["last_touched_round"]),
            experiment_refs=tuple(data.get("experiment_refs") or ()),
            parent_finding_id=data.get("parent_finding_id"),
            stats=dict(data.get("stats") or {}),
        )
