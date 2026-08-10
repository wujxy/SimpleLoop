"""Domain state for one Scientist inquiry.

The immutable artifacts describe scientific commitments. Mutable context state
contains only evidence and guards for one epistemic frame; cumulative telemetry
lives on the enclosing session so a fresh reframe cannot inherit stale guards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .research_agent import WorkingState


MAX_FRESH_REFRAMES = 1


class InquiryPhase(str, Enum):
    UNDERSTAND = "understand"
    MODEL = "model"
    EXPLAIN = "explain"
    EXPLORE = "explore"
    NARROW = "narrow"
    DEEPEN = "deepen"


@dataclass(frozen=True)
class Understanding:
    problem: str
    target_outcome: str
    boundary: str
    current_account_of_the_whole: str
    key_unknowns: tuple[str, ...]


@dataclass(frozen=True)
class ModelClaim:
    id: str
    claim: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class WorkingModel:
    version: int
    representation: str
    explanatory_structure: str
    claims: tuple[ModelClaim, ...]
    important_unknowns: tuple[str, ...]


@dataclass(frozen=True)
class Explanation:
    id: str
    phenomenon: str
    account: str
    model_basis: tuple[str, ...]
    expected_if_true: tuple[str, ...]
    evidence_needed: tuple[str, ...]


@dataclass(frozen=True)
class LeveragePoint:
    id: str
    target_mechanism: str
    why_leverage_exists: str
    model_basis: tuple[str, ...]
    explanation_basis: tuple[str, ...]


@dataclass(frozen=True)
class ResearchHypothesis:
    id: str
    generative_op: str | None
    model_basis: tuple[str, ...]
    explanation_basis: tuple[str, ...]
    mechanism: str
    intervention_family: str
    scope: str
    why_plausible: str
    critical_unknown: str
    evidence_refs: tuple[str, ...] = ()

    def signature(self) -> tuple[str, str, str]:
        return tuple(
            " ".join(value.lower().split())
            for value in (self.mechanism, self.intervention_family, self.scope)
        )


@dataclass(frozen=True)
class HypothesisSelection:
    hypothesis_id: str
    evidence_refs: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class PhaseTransition:
    context_id: int
    source: InquiryPhase
    target: InquiryPhase
    step: int
    reason: str
    history_visible: bool


@dataclass
class InquiryState:
    context_id: int = 0
    phase: InquiryPhase = InquiryPhase.UNDERSTAND
    history_visible: bool = False
    history_injected_at_step: int | None = None
    understanding: Understanding | None = None
    working_model: WorkingModel | None = None
    model_revisions: list[WorkingModel] = field(default_factory=list)
    explanations: list[Explanation] = field(default_factory=list)
    lever_map: list[LeveragePoint] = field(default_factory=list)
    hypotheses: list[ResearchHypothesis] = field(default_factory=list)
    selections: list[HypothesisSelection] = field(default_factory=list)
    proposals: list[object] = field(default_factory=list)
    phase_transitions: list[PhaseTransition] = field(default_factory=list)
    narrow_decisions: list[dict] = field(default_factory=list)
    deep_evidence_refs: set[str] = field(default_factory=set)
    reopen_counts: dict[str, int] = field(default_factory=dict)

    def transition(
        self, target: InquiryPhase, *, step: int, reason: str,
    ) -> None:
        self.phase_transitions.append(PhaseTransition(
            context_id=self.context_id,
            source=self.phase,
            target=target,
            step=step,
            reason=reason,
            history_visible=self.history_visible,
        ))
        self.phase = target

    def set_history_visible(self, visible: bool, *, step: int) -> None:
        if self.history_visible and not visible:
            raise ValueError("history cannot be hidden in the same context")
        if visible and not self.history_visible:
            self.history_visible = True
            self.history_injected_at_step = step


@dataclass
class ScientistContextState:
    runtime: WorkingState = field(default_factory=WorkingState)
    inquiry: InquiryState = field(default_factory=InquiryState)

    @classmethod
    def fresh(cls, *, context_id: int = 0) -> "ScientistContextState":
        return cls(inquiry=InquiryState(context_id=context_id))


@dataclass
class ScientistSessionState:
    context: ScientistContextState = field(default_factory=ScientistContextState.fresh)
    archived_contexts: list[ScientistContextState] = field(default_factory=list)
    cumulative_usage: list[dict] = field(default_factory=list)
    cumulative_action_log: list[dict] = field(default_factory=list)
    usage_by_phase: dict[str, list] = field(default_factory=dict)
    fresh_reframes: int = 0

    @classmethod
    def fresh(cls) -> "ScientistSessionState":
        return cls()

    @property
    def inquiry(self) -> InquiryState:
        return self.context.inquiry

    @property
    def runtime(self) -> WorkingState:
        return self.context.runtime

    def start_fresh_context(self) -> None:
        if self.fresh_reframes >= MAX_FRESH_REFRAMES:
            raise ValueError("fresh reframe limit exceeded")
        context_id = self.inquiry.context_id + 1
        self.archived_contexts.append(self.context)
        self.context = ScientistContextState.fresh(context_id=context_id)
        self.fresh_reframes += 1
