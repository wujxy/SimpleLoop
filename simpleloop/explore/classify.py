"""Objective classification for the Explore module.

Moved verbatim from ``memory/signals.py`` (the text utilities and the
per-experiment classification rules) and extended with ``classify_experiments``,
a single pass that returns one :class:`ObjectiveClassification` per experiment —
consumed by the monitor, the families aggregator, and the legacy
Explore health analysis.

Classification rules (unchanged from the original):

- A gate failure is treated as *feasibility* (the experiment did not
  effectively test the mechanism), not a mechanism refutation: it is left
  ``unclassified`` rather than counted as a regression.
- ``selected`` is contaminated by sibling competition, so eligible attempts are
  classified against their *parent* objective, not by whether they won.
- Parent objective is recovered from a ``sha -> objective`` map built from
  candidate rows; experiments whose parent is not in the map are
  ``unclassified`` (with ``parent_objective=None``) rather than force-fit.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

from .models import (
    KIND_IMPROVEMENT,
    KIND_NEUTRAL,
    KIND_REGRESSION,
    KIND_UNCLASSIFIED,
    ObjectiveClassification,
)

if TYPE_CHECKING:
    # Importing memory here at runtime would create a cycle
    # (memory.__init__ -> service -> explore). These modules only duck-type
    # Experiment/Finding attributes, never isinstance them, and annotations
    # are deferred via ``from __future__ import annotations``.
    from ..memory.experiment_index import Experiment


# --- text utilities (also used by the proposer's near-duplicate check) ------

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> frozenset[str]:
    """Coarse, deterministic tokenization for overlap comparison.

    Lowercased alphanumeric tokens; stop-word filtering is intentionally
    absent so the comparison stays a blunt lexical signal, not a semantic one.
    """
    if not text:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(text.lower()))


def jaccard_overlap(a: str, b: str) -> float:
    """Jaccard similarity over token sets. 0.0 when either is empty."""
    sa, sb = tokenize(a), tokenize(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# --- per-experiment objective classification ------------------------------

def sha_objective_map(
    experiments: Iterable[Experiment], objective_key: str,
) -> dict[str, float]:
    """Map each candidate sha to its objective value. Only candidates that
    produced a sha and a numeric objective contribute; the parent of a later
    experiment is resolved through this map."""
    out: dict[str, float] = {}
    for exp in experiments:
        sha = exp.candidate_sha
        if not sha:
            continue
        value = exp.metrics.get(objective_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[sha] = float(value)
    return out


def classify_objective(
    obj: float, parent_obj: float, *, lower_is_better: bool,
) -> str:
    """Return ``improvement`` / ``neutral`` / ``regression`` for an eligible
    experiment's objective relative to its parent, using a relative tolerance
    so float noise is not read as a change."""
    eps = abs(parent_obj) * 1e-6 + 1e-9
    delta = obj - parent_obj
    if lower_is_better:
        improved = delta < -eps
        regressed = delta > eps
    else:
        improved = delta > eps
        regressed = delta < -eps
    if improved:
        return KIND_IMPROVEMENT
    if regressed:
        return KIND_REGRESSION
    return KIND_NEUTRAL


def classify_experiments(
    experiments: list[Experiment],
    objective_key: str,
    *,
    lower_is_better: bool,
) -> list[ObjectiveClassification]:
    """Classify every experiment relative to its parent objective in one pass.

    Returns one :class:`ObjectiveClassification` per experiment in input order.
    Non-eligible experiments (gate failures) are ``unclassified`` with
    ``objective`` and ``parent_objective`` carried through for traceability but
    ``eligible=False``. Eligible experiments with an unresolvable parent are
    ``unclassified`` with ``parent_objective=None``.
    """
    sha_obj = sha_objective_map(experiments, objective_key)
    out: list[ObjectiveClassification] = []
    for exp in experiments:
        raw_obj = exp.metrics.get(objective_key)
        obj = (
            float(raw_obj)
            if isinstance(raw_obj, (int, float)) and not isinstance(raw_obj, bool)
            else None
        )
        parent_obj = sha_obj.get(exp.parent_sha)
        parent_obj_f = (
            float(parent_obj)
            if isinstance(parent_obj, (int, float))
            and not isinstance(parent_obj, bool)
            else None
        )
        if not exp.eligible:
            kind = KIND_UNCLASSIFIED
        elif obj is None or parent_obj_f is None:
            # Unparseable own objective or unresolvable parent (e.g. baseline)
            # — not force-fit into a fake improvement.
            kind = KIND_UNCLASSIFIED
        else:
            kind = classify_objective(
                obj, parent_obj_f, lower_is_better=lower_is_better,
            )
        out.append(ObjectiveClassification(
            experiment_id=exp.experiment_id,
            round=exp.round,
            candidate=exp.candidate,
            finding_id=exp.finding_id,
            parent_sha=exp.parent_sha,
            candidate_sha=exp.candidate_sha,
            objective=obj,
            parent_objective=parent_obj_f,
            eligible=exp.eligible,
            kind=kind,
        ))
    return out
