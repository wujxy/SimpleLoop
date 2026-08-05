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
    render_challenge_repair_message,
    render_explore_for_state_header,
    render_explore_for_startup,
)

__all__ = [
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
    "render_challenge_repair_message",
    "render_explore_for_state_header",
    "render_explore_for_startup",
    "sha_objective_map",
    "tokenize",
    "UNKNOWN_MECHANISM",
    "UNKNOWN_REGION",
]
