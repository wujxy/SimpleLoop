"""Data models for the Explore (search-health) module.

Explore is a *read-only* monitor over the immutable Experiment Ledger and the
Finding archive. It classifies each experiment relative to its parent objective,
then aggregates health signals at three levels:

- **per-finding** — the legacy ``feasibility_risk`` / ``mechanism_challenge`` /
  ``contradictory_result`` nudges.
- **family** — the new layer that survives "open a fresh Finding each round":
  experiments are grouped by a deterministic code-region bucket plus a
  canonicalized mechanism bucket, so rewording a mechanism or renaming the
  question does not escape detection.
- **global** — the cross-finding run-length view of recent rounds.

Everything here is a frozen value object: no LLM verdicts, no schema writes, no
``summary``/``recommendation`` fields that would compete with the Ledger. See
``docs/explore_refactor_plan.md``.
"""
from __future__ import annotations

from dataclasses import dataclass


# Objective classification kinds.
KIND_IMPROVEMENT = "improvement"
KIND_NEUTRAL = "neutral"
KIND_REGRESSION = "regression"
KIND_UNCLASSIFIED = "unclassified"

# Policy signal severities. ``challenge`` severity feeds ``challenge_required``
# (informational only — does not gate submit); ``watch``/``info`` are nudges.
SEVERITY_INFO = "info"
SEVERITY_WATCH = "watch"
SEVERITY_CHALLENGE = "challenge"


@dataclass(frozen=True)
class ObjectiveClassification:
    """One experiment classified relative to its parent objective.

    ``kind`` is one of ``improvement`` / ``neutral`` / ``regression`` /
    ``unclassified``. An experiment is ``unclassified`` when it is not eligible
    (a gate failure is *feasibility*, not a mechanism refutation) or when its
    parent objective cannot be resolved from the candidate rows — such rows are
    never force-fit into a fake improvement. ``eligible`` distinguishes the two
    unclassified causes so callers can separate feasibility stalls from
    unresolvable parents.
    """

    experiment_id: str
    round: int
    candidate: int
    finding_id: str | None
    parent_sha: str
    candidate_sha: str | None
    objective: float | None
    parent_objective: float | None
    eligible: bool
    kind: str

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "round": self.round,
            "candidate": self.candidate,
            "finding_id": self.finding_id,
            "parent_sha": self.parent_sha,
            "candidate_sha": self.candidate_sha,
            "objective": self.objective,
            "parent_objective": self.parent_objective,
            "eligible": self.eligible,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class PolicySignal:
    """A threshold-derived nudge with its rule attached, so the Scientist never
    mistakes a harness heuristic for an experimental conclusion."""

    name: str
    active: bool
    rule: str
    severity: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "active": self.active,
            "rule": self.rule,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class FindingExploreHealth:
    finding_id: str
    question: str
    attempts: int
    evaluable_attempts: int
    implementation_failures: int
    improvements: int
    neutral: int
    regressions: int
    selected: int
    last_touched_round: int
    policy_signals: tuple[PolicySignal, ...] = ()

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "question": self.question,
            "attempts": self.attempts,
            "evaluable_attempts": self.evaluable_attempts,
            "implementation_failures": self.implementation_failures,
            "improvements": self.improvements,
            "neutral": self.neutral,
            "regressions": self.regressions,
            "selected": self.selected,
            "last_touched_round": self.last_touched_round,
            "policy_signals": [s.to_dict() for s in self.policy_signals],
        }


@dataclass(frozen=True)
class FamilyExploreHealth:
    """A mechanism/region family aggregated *across* findings.

    This is the layer that defeats "open a fresh Finding each round": every
    finding whose tags bucket to the same ``(code_region, mechanism)`` lands in
    one family, so a stalled family is visible even when each individual finding
    has only one experiment. ``consecutive_no_improve`` walks the family's
    attempts in time order, stopping at the first improvement; gaps across
    rounds do not reset it.
    """

    family_id: str
    code_region: str
    mechanisms: tuple[str, ...]
    finding_ids: tuple[str, ...]
    attempts: int
    evaluable_attempts: int
    implementation_failures: int
    improvements: int
    neutral: int
    regressions: int
    selected: int
    consecutive_no_improve: int
    recent_rounds: tuple[int, ...]
    policy_signals: tuple[PolicySignal, ...] = ()

    def to_dict(self) -> dict:
        return {
            "family_id": self.family_id,
            "code_region": self.code_region,
            "mechanisms": list(self.mechanisms),
            "finding_ids": list(self.finding_ids),
            "attempts": self.attempts,
            "evaluable_attempts": self.evaluable_attempts,
            "implementation_failures": self.implementation_failures,
            "improvements": self.improvements,
            "neutral": self.neutral,
            "regressions": self.regressions,
            "selected": self.selected,
            "consecutive_no_improve": self.consecutive_no_improve,
            "recent_rounds": list(self.recent_rounds),
            "policy_signals": [s.to_dict() for s in self.policy_signals],
        }


@dataclass(frozen=True)
class GlobalExploreHealth:
    """The cross-finding view of the most recent rounds.

    ``consecutive_no_improve_rounds`` is a run-length walk over rounds (latest
    first, stopping at the first round with any classifiable improvement),
    while the ``recent_*`` counts are over a fixed ``recent_window`` for display
    and the ``global_regression_run`` basis.
    """

    attempts: int
    evaluable_attempts: int
    recent_window: int
    recent_improvements: int
    recent_neutral: int
    recent_regressions: int
    consecutive_no_improve_rounds: int
    recent_mechanisms: tuple[str, ...]
    policy_signals: tuple[PolicySignal, ...] = ()

    def to_dict(self) -> dict:
        return {
            "attempts": self.attempts,
            "evaluable_attempts": self.evaluable_attempts,
            "recent_window": self.recent_window,
            "recent_improvements": self.recent_improvements,
            "recent_neutral": self.recent_neutral,
            "recent_regressions": self.recent_regressions,
            "consecutive_no_improve_rounds": self.consecutive_no_improve_rounds,
            "recent_mechanisms": list(self.recent_mechanisms),
            "policy_signals": [s.to_dict() for s in self.policy_signals],
        }


@dataclass(frozen=True)
class ExploreReport:
    """The full search-health readout computed once at Proposer wakeup.

    ``first_round`` is purely ``not experiments``. When an objective key is
    missing, ``analysis_eligible`` is False: fact counts that do not need the
    objective are still produced, but no classification-derived counts and no
    policy signals. ``challenge_required`` is True iff any family/global signal
    of ``challenge`` severity is active — per-finding signals never set it. The
    flag is informational context for the agent; it does not gate submit.
    """

    first_round: bool
    analysis_eligible: bool
    findings: tuple[FindingExploreHealth, ...] = ()
    families: tuple[FamilyExploreHealth, ...] = ()
    global_health: GlobalExploreHealth | None = None
    challenge_required: bool = False
    challenge_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "first_round": self.first_round,
            "analysis_eligible": self.analysis_eligible,
            "findings": [f.to_dict() for f in self.findings],
            "families": [f.to_dict() for f in self.families],
            "global_health": (
                self.global_health.to_dict() if self.global_health else None
            ),
            "challenge_required": self.challenge_required,
            "challenge_reasons": list(self.challenge_reasons),
        }

    def active_challenge_signals(self) -> list[PolicySignal]:
        """All active ``challenge``-severity signals across families and
        global, for telemetry/trace."""
        out: list[PolicySignal] = []
        for fam in self.families:
            out.extend(
                s for s in fam.policy_signals
                if s.active and s.severity == SEVERITY_CHALLENGE
            )
        if self.global_health:
            out.extend(
                s for s in self.global_health.policy_signals
                if s.active and s.severity == SEVERITY_CHALLENGE
            )
        return out
