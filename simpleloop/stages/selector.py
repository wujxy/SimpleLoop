"""Config-free objective selection for one candidate batch."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..candidate import CandidateResult


@dataclass(frozen=True)
class Selection:
    candidate_id: int | None
    sha: str | None
    reason: str


def select_candidate(
    *,
    candidates: tuple[CandidateResult, ...],
    objective_key: str,
    lower_is_better: bool,
    incumbent_value: float | None,
    require_improvement: bool = True,
) -> Selection:
    eligible = []
    for candidate in candidates:
        value = candidate.metrics.get(objective_key)
        numeric = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
        if candidate.eligible and candidate.sha and numeric:
            eligible.append((candidate, float(value)))
    if not eligible:
        return Selection(None, None, "no_eligible_candidate")
    direction = 1 if lower_is_better else -1
    winner, winner_value = min(
        eligible,
        key=lambda item: (direction * item[1], item[0].candidate_id),
    )
    if require_improvement and incumbent_value is not None:
        improved = (
            winner_value < incumbent_value
            if lower_is_better else winner_value > incumbent_value
        )
        if not improved:
            return Selection(None, None, "no_improvement")
    return Selection(winner.candidate_id, winner.sha, "selected")
