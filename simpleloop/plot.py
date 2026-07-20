"""Per-round score and objective progress plotting."""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


_MPL_CACHE = Path(tempfile.gettempdir()) / "simpleloop-matplotlib"
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))


@dataclass
class PlotSeries:
    rounds: list[int]
    score_points: list[tuple[int, float]]
    selected_scores: list[tuple[int, float]]
    objective_points: list[tuple[int, float]]
    selected_objectives: list[tuple[int, float]]
    incumbent_objective: list[tuple[int, float]]
    objective_key: str | None
    lower_is_better: bool | None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_series(history: list[dict], metrics_schema: dict | None) -> PlotSeries:
    """Transform persisted serial or parallel history into chart series."""
    objective = (metrics_schema or {}).get("objective") or {}
    objective_key = objective.get("key")
    lower_is_better = objective.get("lower_is_better")
    if not isinstance(lower_is_better, bool):
        lower_is_better = None

    rounds: list[int] = []
    score_points: list[tuple[int, float]] = []
    selected_scores: list[tuple[int, float]] = []
    objective_points: list[tuple[int, float]] = []
    selected_objectives: list[tuple[int, float]] = []
    incumbent_objective: list[tuple[int, float]] = []
    incumbent: float | None = None

    for index, record in enumerate(history):
        round_number = int(record.get("round", index)) + 1
        rounds.append(round_number)
        parallel = isinstance(record.get("candidates"), list)
        candidates = record["candidates"] if parallel else [record]
        selected_id = record.get("selected_candidate") if parallel else None

        for candidate in candidates:
            selected = (
                selected_id is not None
                and candidate.get("candidate") == selected_id
                if parallel
                else bool(record.get("accepted"))
            )
            score = candidate.get("score")
            if _is_number(score):
                score_point = (round_number, float(score))
                score_points.append(score_point)
                if selected:
                    selected_scores.append(score_point)

            value = (candidate.get("metrics") or {}).get(objective_key)
            if _is_number(value):
                objective_point = (round_number, float(value))
                objective_points.append(objective_point)
                if selected:
                    selected_objectives.append(objective_point)
                    incumbent = float(value)

        if incumbent is not None:
            incumbent_objective.append((round_number, incumbent))

    return PlotSeries(
        rounds=rounds,
        score_points=score_points,
        selected_scores=selected_scores,
        objective_points=objective_points,
        selected_objectives=selected_objectives,
        incumbent_objective=incumbent_objective,
        objective_key=objective_key,
        lower_is_better=lower_is_better,
    )


def _xy(points: list[tuple[int, float]]) -> tuple[list[int], list[float]]:
    return [point[0] for point in points], [point[1] for point in points]


def _round_ticks(rounds: list[int], limit: int = 12) -> list[int]:
    if len(rounds) <= limit:
        return rounds
    step = max(1, math.ceil(len(rounds) / limit))
    ticks = rounds[::step]
    if ticks[-1] != rounds[-1]:
        ticks.append(rounds[-1])
    return ticks


def _render_progress_png(series: PlotSeries, output: Path) -> None:
    _MPL_CACHE.mkdir(parents=True, exist_ok=True)
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    figure, (score_axis, objective_axis) = plt.subplots(
        2,
        1,
        figsize=(12, 8),
        sharex=True,
    )
    figure.patch.set_facecolor("white")

    score_x, score_y = _xy(series.score_points)
    if score_x:
        score_axis.scatter(
            score_x,
            score_y,
            color="#9AA3AD",
            alpha=0.65,
            s=34,
            label="All candidates",
            zorder=2,
        )
    selected_score_x, selected_score_y = _xy(series.selected_scores)
    if selected_score_x:
        score_axis.plot(
            selected_score_x,
            selected_score_y,
            color="#147D73",
            linewidth=2.2,
            marker="o",
            markersize=6,
            label="Selected",
            zorder=3,
        )
    if not score_x:
        score_axis.text(
            0.5,
            0.5,
            "No score data",
            ha="center",
            va="center",
            transform=score_axis.transAxes,
            color="#6B7280",
        )
    score_axis.set_title("Judger score")
    score_axis.set_ylabel("Score")
    score_axis.set_ylim(-0.02, 1.02)
    score_axis.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
    if score_axis.get_legend_handles_labels()[0]:
        score_axis.legend(loc="best", frameon=False)

    objective_x, objective_y = _xy(series.objective_points)
    if objective_x:
        objective_axis.scatter(
            objective_x,
            objective_y,
            color="#D88932",
            alpha=0.65,
            s=34,
            label="All candidates",
            zorder=2,
        )
    selected_objective_x, selected_objective_y = _xy(series.selected_objectives)
    if selected_objective_x:
        objective_axis.scatter(
            selected_objective_x,
            selected_objective_y,
            color="#B43A3A",
            marker="D",
            s=48,
            label="Selected",
            zorder=4,
        )
    incumbent_x, incumbent_y = _xy(series.incumbent_objective)
    if incumbent_x:
        objective_axis.step(
            incumbent_x,
            incumbent_y,
            where="post",
            color="#2856A6",
            linewidth=2.4,
            label="Accepted incumbent",
            zorder=3,
        )
    if not objective_x:
        objective_axis.text(
            0.5,
            0.5,
            "No objective data",
            ha="center",
            va="center",
            transform=objective_axis.transAxes,
            color="#6B7280",
        )

    direction = (
        "lower is better"
        if series.lower_is_better is True
        else "higher is better"
        if series.lower_is_better is False
        else "direction not configured"
    )
    objective_name = series.objective_key or "Objective"
    objective_axis.set_title(f"{objective_name} ({direction})")
    objective_axis.set_ylabel(objective_name)
    objective_axis.set_xlabel("Round")
    objective_axis.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
    if objective_axis.get_legend_handles_labels()[0]:
        objective_axis.legend(loc="best", frameon=False)

    if series.rounds:
        objective_axis.set_xticks(_round_ticks(series.rounds))
        objective_axis.set_xlim(series.rounds[0] - 0.5, series.rounds[-1] + 0.5)

    figure.suptitle("SimpleLoop optimization progress", fontsize=15)
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    try:
        figure.savefig(output, format="png", dpi=150)
    finally:
        plt.close(figure)


def write_progress_png(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
) -> Path | None:
    """Atomically redraw progress.png; plotting failures are non-fatal."""
    run_path = Path(run_dir)
    output = run_path / "progress.png"
    temporary = run_path / ".progress.tmp.png"
    try:
        run_path.mkdir(parents=True, exist_ok=True)
        series = build_series(history, metrics_schema)
        _render_progress_png(series, temporary)
        os.replace(temporary, output)
        return output
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"[plot] warning: could not update {output}: {exc}", flush=True)
        return None
