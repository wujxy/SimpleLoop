"""Progress plots across round, active worktime, and processed tokens."""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_MPL_CACHE = Path(tempfile.gettempdir()) / "simpleloop-matplotlib"
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))

_Y_KINDS = ("score", "objective", "ratio")
_X_KINDS = ("round", "worktime", "tokens")
_Y_SLUGS = {
    "score": "score",
    "objective": "objective",
    "ratio": "objective-ratio",
}


@dataclass
class Observation:
    round: float
    worktime_hours: float | None
    processed_tokens: int | None
    score: float | None
    objective: float | None
    ratio: float | None
    selected: bool = False


@dataclass
class PlotSeries:
    candidates: list[Observation]
    incumbents: list[Observation]
    baseline: Observation | None
    objective_key: str | None
    lower_is_better: bool | None
    rounds: list[int]

    # Preserve the small public transformation surface used by existing callers.
    @property
    def score_points(self) -> list[tuple[int, float]]:
        return [
            (int(point.round), point.score)
            for point in self.candidates
            if point.score is not None
        ]

    @property
    def selected_scores(self) -> list[tuple[int, float]]:
        return [
            (int(point.round), point.score)
            for point in self.candidates
            if point.selected and point.score is not None
        ]

    @property
    def objective_points(self) -> list[tuple[int, float]]:
        return [
            (int(point.round), point.objective)
            for point in self.candidates
            if point.objective is not None
        ]

    @property
    def selected_objectives(self) -> list[tuple[int, float]]:
        return [
            (int(point.round), point.objective)
            for point in self.candidates
            if point.selected and point.objective is not None
        ]

    @property
    def incumbent_objective(self) -> list[tuple[int, float]]:
        return [
            (int(point.round), point.objective)
            for point in self.incumbents
            if point.round > 0 and point.objective is not None
        ]


def _number(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        return float(value)
    return None


def _tokens(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _coordinates(telemetry: object) -> tuple[float | None, int | None]:
    if not isinstance(telemetry, dict):
        return None, None
    seconds = _number(telemetry.get("worktime_seconds"))
    if seconds is not None and seconds < 0:
        seconds = None
    return (
        seconds / 3600.0 if seconds is not None else None,
        _tokens(telemetry.get("processed_tokens")),
    )


def _observation(
    *,
    round_number: float,
    telemetry: object,
    score: object = None,
    objective: object = None,
    baseline_value: float | None = None,
    selected: bool = False,
) -> Observation:
    worktime, tokens = _coordinates(telemetry)
    objective_value = _number(objective)
    ratio = (
        objective_value / baseline_value
        if objective_value is not None and baseline_value not in (None, 0.0)
        else None
    )
    return Observation(
        round=round_number,
        worktime_hours=worktime,
        processed_tokens=tokens,
        score=_number(score),
        objective=objective_value,
        ratio=ratio,
        selected=selected,
    )


def build_series(
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> PlotSeries:
    """Transform persisted history and baseline context into plot observations."""
    objective = (metrics_schema or {}).get("objective") or {}
    objective_key = objective.get("key")
    lower_is_better = objective.get("lower_is_better")
    if not isinstance(lower_is_better, bool):
        lower_is_better = None

    context = plot_context or {}
    baseline_metrics = context.get("baseline_metrics") or {}
    baseline_value = _number(baseline_metrics.get(objective_key))
    if baseline_value == 0.0:
        baseline_value = None
    baseline = None
    if baseline_value is not None:
        baseline = _observation(
            round_number=0,
            telemetry=context.get("baseline_telemetry"),
            objective=baseline_value,
            baseline_value=baseline_value,
            selected=True,
        )

    candidates: list[Observation] = []
    incumbents: list[Observation] = [baseline] if baseline is not None else []
    rounds: list[int] = []
    incumbent_value = baseline_value

    for index, record in enumerate(history):
        round_number = int(record.get("round", index)) + 1
        rounds.append(round_number)
        parallel = isinstance(record.get("candidates"), list)
        attempts = record["candidates"] if parallel else [record]
        selected_id = record.get("selected_candidate") if parallel else None
        selected_observation: Observation | None = None

        for attempt in attempts:
            selected = (
                attempt.get("candidate") == selected_id
                if parallel
                else bool(record.get("accepted"))
            )
            metrics = attempt.get("metrics") or {}
            point = _observation(
                round_number=round_number,
                telemetry=attempt.get("telemetry"),
                score=attempt.get("score"),
                objective=metrics.get(objective_key),
                baseline_value=baseline_value,
                selected=selected,
            )
            candidates.append(point)
            if selected:
                selected_observation = point

        accepted = bool(record.get("selected_sha")) if parallel else bool(
            record.get("accepted")
        )
        if (
            accepted
            and selected_observation is not None
            and selected_observation.objective is not None
        ):
            incumbent_value = selected_observation.objective

        if incumbent_value is not None:
            incumbents.append(_observation(
                round_number=round_number,
                telemetry=record.get("telemetry"),
                objective=incumbent_value,
                baseline_value=baseline_value,
                selected=True,
            ))

    return PlotSeries(
        candidates=candidates,
        incumbents=incumbents,
        baseline=baseline,
        objective_key=objective_key,
        lower_is_better=lower_is_better,
        rounds=rounds,
    )


def _x(point: Observation, kind: str) -> float | int | None:
    if kind == "round":
        return point.round
    if kind == "worktime":
        return point.worktime_hours
    return point.processed_tokens


def _y(point: Observation, kind: str) -> float | None:
    if kind == "score":
        return point.score
    if kind == "objective":
        return point.objective
    return point.ratio


def _xy(
    points: list[Observation],
    y_kind: str,
    x_kind: str,
) -> tuple[list[float | int], list[float]]:
    pairs = [
        (_x(point, x_kind), _y(point, y_kind))
        for point in points
    ]
    valid = [(x, y) for x, y in pairs if x is not None and y is not None]
    return [x for x, _ in valid], [y for _, y in valid]


def _round_ticks(rounds: list[int], limit: int = 12) -> list[int]:
    values = [0] + rounds if rounds else [0]
    if len(values) <= limit:
        return values
    step = max(1, math.ceil(len(values) / limit))
    ticks = values[::step]
    if ticks[-1] != values[-1]:
        ticks.append(values[-1])
    return ticks


def _x_label(kind: str) -> str:
    return {
        "round": "Round",
        "worktime": "Cumulative worktime (hours)",
        "tokens": "Cumulative processed tokens",
    }[kind]


def _y_label(series: PlotSeries, kind: str) -> str:
    if kind == "score":
        return "Score"
    key = series.objective_key or "Objective"
    if kind == "ratio":
        return f"{key} ratio (vs baseline)"
    return key


def _render_panel(axis, series: PlotSeries, y_kind: str, x_kind: str) -> None:
    candidate_x, candidate_y = _xy(series.candidates, y_kind, x_kind)
    if candidate_x:
        axis.scatter(
            candidate_x,
            candidate_y,
            color="#9AA3AD" if y_kind == "score" else "#D88932",
            alpha=0.65,
            s=30,
            label="All candidates",
            zorder=2,
        )

    selected = [point for point in series.candidates if point.selected]
    selected_x, selected_y = _xy(selected, y_kind, x_kind)
    if selected_x:
        if y_kind == "score":
            axis.plot(
                selected_x,
                selected_y,
                color="#147D73",
                linewidth=2.0,
                marker="o",
                markersize=5,
                label="Selected",
                zorder=3,
            )
        else:
            axis.scatter(
                selected_x,
                selected_y,
                color="#B43A3A",
                marker="D",
                s=42,
                label="Selected",
                zorder=4,
            )

    if y_kind in ("objective", "ratio"):
        incumbent_x, incumbent_y = _xy(series.incumbents, y_kind, x_kind)
        if incumbent_x:
            axis.step(
                incumbent_x,
                incumbent_y,
                where="post",
                color="#2856A6",
                linewidth=2.2,
                label="Accepted incumbent",
                zorder=3,
            )
    if y_kind == "ratio":
        axis.axhline(
            1.0,
            color="#6B7280",
            linestyle="--",
            linewidth=1.0,
            label="Baseline",
            zorder=1,
        )

    has_data = bool(candidate_x)
    if y_kind in ("objective", "ratio"):
        incumbent_x, _ = _xy(series.incumbents, y_kind, x_kind)
        has_data = has_data or bool(incumbent_x)
    if not has_data:
        axis.text(
            0.5,
            0.5,
            "No data",
            ha="center",
            va="center",
            transform=axis.transAxes,
            color="#6B7280",
        )

    axis.set_xlabel(_x_label(x_kind))
    axis.set_ylabel(_y_label(series, y_kind))
    axis.set_title(f"{_y_label(series, y_kind)} vs {_x_label(x_kind)}")
    axis.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
    if y_kind == "score":
        axis.set_ylim(0.0, 1.0)
    if x_kind == "round":
        axis.set_xticks(_round_ticks(series.rounds))
        if series.rounds:
            axis.set_xlim(-0.5, series.rounds[-1] + 0.5)
    handles, _ = axis.get_legend_handles_labels()
    if handles:
        axis.legend(loc="best", frameon=False, fontsize="small")


def _prepare_pyplot():
    _MPL_CACHE.mkdir(parents=True, exist_ok=True)
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    return plt


def _render_progress_png(series: PlotSeries, output: Path) -> None:
    plt = _prepare_pyplot()
    figure, axes = plt.subplots(3, 3, figsize=(18, 13))
    figure.patch.set_facecolor("white")
    for row, y_kind in enumerate(_Y_KINDS):
        for column, x_kind in enumerate(_X_KINDS):
            _render_panel(axes[row][column], series, y_kind, x_kind)
    figure.suptitle("SimpleLoop optimization progress", fontsize=16)
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    try:
        figure.savefig(output, format="png", dpi=140)
    finally:
        plt.close(figure)


def _render_detail(
    series: PlotSeries,
    y_kind: str,
    x_kind: str,
    output: Path,
) -> None:
    plt = _prepare_pyplot()
    figure, axis = plt.subplots(figsize=(9, 5.5))
    figure.patch.set_facecolor("white")
    _render_panel(axis, series, y_kind, x_kind)
    figure.tight_layout()
    try:
        figure.savefig(output, format="png", dpi=140)
    finally:
        plt.close(figure)


def _publish(output: Path, render: Callable[[Path], None]) -> bool:
    temporary = output.with_name(f".{output.stem}.tmp{output.suffix}")
    try:
        render(temporary)
        os.replace(temporary, output)
        return True
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"[plot] warning: could not update {output}: {exc}", flush=True)
        return False


def write_progress_pngs(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> list[Path]:
    """Redraw the overview and nine detail images independently."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    series = build_series(history, metrics_schema, plot_context)
    published: list[Path] = []

    overview = run_path / "progress.png"
    if _publish(overview, lambda path: _render_progress_png(series, path)):
        published.append(overview)

    for y_kind in _Y_KINDS:
        for x_kind in _X_KINDS:
            output = run_path / (
                f"progress-{_Y_SLUGS[y_kind]}-vs-{x_kind}.png"
            )
            if _publish(
                output,
                lambda path, y=y_kind, x=x_kind: _render_detail(
                    series, y, x, path
                ),
            ):
                published.append(output)
    return published


def write_progress_png(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> Path | None:
    """Compatibility entry point; it now refreshes all progress images."""
    overview = Path(run_dir) / "progress.png"
    outputs = write_progress_pngs(
        run_dir,
        history,
        metrics_schema,
        plot_context,
    )
    return overview if overview in outputs else None
