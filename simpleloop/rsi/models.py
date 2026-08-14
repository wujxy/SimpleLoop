"""Mechanism-neutral contracts for recursive self-improvement."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from ..world import SourceWorkspace


class NoSelfChangeError(RuntimeError):
    pass


class SelfDecisionKind(str, Enum):
    KEEP = "KEEP"
    CHANGE = "CHANGE"


class SelfEventKind(str, Enum):
    INITIALIZED = "INITIALIZED"
    REVIEWED_KEEP = "REVIEWED_KEEP"
    REVIEWED_CHANGE = "REVIEWED_CHANGE"
    CANDIDATE_CREATED = "CANDIDATE_CREATED"
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
    CANDIDATE_ADOPTED = "CANDIDATE_ADOPTED"


@dataclass(frozen=True)
class SelfRevision:
    sha: str

    def __post_init__(self) -> None:
        if not self.sha:
            raise ValueError("self revision sha must be non-empty")


@dataclass(frozen=True)
class SelfChange:
    target: str
    intent: str
    instruction: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelfDecision:
    kind: SelfDecisionKind
    diagnosis: str
    keep_reason: str | None = None
    change: SelfChange | None = None
    next_review_after_rounds: int | None = None

    def __post_init__(self) -> None:
        if self.kind is SelfDecisionKind.CHANGE and self.change is None:
            raise ValueError("CHANGE requires a self change")
        if self.kind is SelfDecisionKind.KEEP and self.change is not None:
            raise ValueError("KEEP cannot carry a self change")
        defer = self.next_review_after_rounds
        if defer is not None and (
            not isinstance(defer, int) or isinstance(defer, bool) or defer < 1
        ):
            raise ValueError("next review defer must be a positive integer")


@dataclass(frozen=True)
class SelfState:
    active_sha: str
    next_review_round: int | None
    last_review_round: int | None


@dataclass(frozen=True)
class SelfEvent:
    event_id: str
    kind: SelfEventKind
    round_id: int
    incumbent_sha: str
    decision: SelfDecision | None = None
    candidate_sha: str | None = None
    next_review_round: int | None = None
    viable: bool | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("self event_id must be non-empty")
        if not self.incumbent_sha:
            raise ValueError("self event incumbent_sha must be non-empty")
        if (
            self.kind is SelfEventKind.CANDIDATE_ADOPTED
            and not self.candidate_sha
        ):
            raise ValueError("CANDIDATE_ADOPTED requires candidate_sha")


@dataclass(frozen=True)
class SelfCandidate:
    parent_sha: str
    sha: str
    changed_paths: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True)
class SelfReviewRequest:
    round_id: int
    goal: str
    incumbent_sha: str
    self_repo: Path
    reviews_path: Path


@dataclass(frozen=True)
class SelfEditRequest:
    round_id: int
    change: SelfChange
    workspace: SourceWorkspace


@dataclass(frozen=True)
class SelfEditResult:
    status: str
    output: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SelfCommitRequest:
    round_id: int
    parent_sha: str
    target: str


@dataclass(frozen=True)
class ViabilityRequest:
    round_id: int
    candidate_sha: str
    self_repo: Path


@dataclass(frozen=True)
class ViabilityResult:
    viable: bool
    detail: str


@dataclass(frozen=True)
class RsiRequest:
    round_id: int
    goal: str


@dataclass(frozen=True)
class RsiResult:
    round_id: int
    decision: SelfDecisionKind
    candidate_sha: str | None = None
    adopted: bool | None = None
    detail: str = ""
