"""Explore — search-health monitor for the Proposer.

Read-only aggregation over the Experiment Ledger and the Finding archive that
tells the Scientist whether its search dynamics are healthy (stalled, trapped
in a local-exploitation family, regressing) without making any scientific
verdict. See ``docs/explore_refactor_plan.md``.
"""
from __future__ import annotations

from .classify import (
    classify_experiments,
    classify_objective,
    jaccard_overlap,
    sha_objective_map,
    tokenize,
)
from .families import (
    UNKNOWN_MECHANISM,
    UNKNOWN_REGION,
    FamilyKey,
    assign_families,
    canonicalize_mechanisms,
)
from .models import (
    ExploreReport,
    FamilyExploreHealth,
    FindingExploreHealth,
    GlobalExploreHealth,
    ObjectiveClassification,
    PolicySignal,
)
from .monitor import analyze_explore_health
from .render import (
    render_explore_for_state_header,
    render_explore_for_startup,
)

__all__ = [
    "analyze_explore_from_schema",
    "analyze_explore_health",
    "assign_families",
    "canonicalize_mechanisms",
    "classify_experiments",
    "classify_objective",
    "ExploreReport",
    "FamilyExploreHealth",
    "FamilyKey",
    "FindingExploreHealth",
    "GlobalExploreHealth",
    "jaccard_overlap",
    "ObjectiveClassification",
    "PolicySignal",
    "render_explore_for_state_header",
    "render_explore_for_startup",
    "sha_objective_map",
    "tokenize",
    "UNKNOWN_MECHANISM",
    "UNKNOWN_REGION",
]


def analyze_explore_from_schema(findings, experiments, *, current_round,
                                metrics_schema):
    """Compute the ``ExploreReport`` from raw ledger/finding data and the run
    metrics schema.

    A thin objective-extracting wrapper over :func:`analyze_explore_health` so
    that the report can be computed by callers that are not ``MemoryService``
    (e.g. the orchestrator/proposer roles) without reaching into private state.
    The report is informational context for the agent; nothing here gates or
    forces any action.
    """
    objective = (metrics_schema or {}).get("objective") or {}
    return analyze_explore_health(
        findings,
        experiments,
        current_round=current_round,
        objective_key=objective.get("key"),
        lower_is_better=bool(objective.get("lower_is_better")),
    )
