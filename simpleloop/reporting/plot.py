"""Progress plots across round, active worktime, and processed tokens.

The loop maintains only the 2x3 overview (write_progress_png); the six
single-panel detail images are drawn offline by scripts/plot_details.py."""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_MPL_CACHE = Path(tempfile.gettempdir()) / "simpleloop-matplotlib"
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))

_Y_KINDS = ("objective", "ratio")
_X_KINDS = ("round", "worktime", "tokens")
_Y_SLUGS = {"objective": "objective", "ratio": "objective-ratio"}
_COST_RMB_PER_MILLION_TOKENS = 28.0
_CACHE_HIT_RMB_PER_MILLION_TOKENS = 2.0
_COST_MODEL_LABEL = "glm-5.2"


@dataclass
class Observation:
    round: float
    worktime_hours: float | None
    processed_tokens: int | None
    objective: float | None
    ratio: float | None
    cost_rmb: float | None = None
    selected: bool = False


@dataclass
class PlotSeries:
    candidates: list[Observation]
    incumbents: list[Observation]
    baseline: Observation | None
    objective_key: str | None
    lower_is_better: bool | None
    rounds: list[int]

    # Convenience views over the observations; exercised by the plot tests.
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


def _cost_rmb(telemetry: object) -> float | None:
    if not isinstance(telemetry, dict):
        return None
    input_tokens = _tokens(telemetry.get("input_tokens"))
    output_tokens = _tokens(telemetry.get("output_tokens"))
    cache_read_tokens = _tokens(telemetry.get("cache_read_input_tokens"))
    cache_creation_tokens = _tokens(telemetry.get("cache_creation_input_tokens"))
    if all(value is None for value in (
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
    )):
        return None
    ordinary_tokens = (input_tokens or 0) + (output_tokens or 0)
    return (
        ordinary_tokens * _COST_RMB_PER_MILLION_TOKENS
        + (cache_read_tokens or 0) * _CACHE_HIT_RMB_PER_MILLION_TOKENS
    ) / 1_000_000.0


def _ratio(
    objective_value: float | None,
    baseline_value: float | None,
    lower_is_better: bool | None,
) -> float | None:
    if objective_value is None or baseline_value in (None, 0.0):
        return None
    # Invert for lower-is-better objectives so the ratio row always reads as an
    # improvement multiple where higher is better.
    if lower_is_better is True:
        if objective_value == 0.0:
            return None
        return baseline_value / objective_value
    return objective_value / baseline_value


def _worktime_rebase_offsets(history: list[dict]) -> list[float]:
    """Per-record worktime offset (hours) lifting post---continue rounds so the
    plotted worktime stays monotonically non-decreasing across sessions."""
    offsets: list[float] = []
    running_hours = 0.0
    ceiling_hours: float | None = None
    for record in history:
        seconds = _number((record.get("telemetry") or {}).get("worktime_seconds"))
        if seconds is not None:
            effective = seconds / 3600.0 + running_hours
            if ceiling_hours is not None and effective < ceiling_hours:
                running_hours += ceiling_hours - effective
                effective = ceiling_hours
            if ceiling_hours is None or effective > ceiling_hours:
                ceiling_hours = effective
        offsets.append(running_hours)
    return offsets


def _observation(
    *,
    round_number: float,
    telemetry: object,
    objective: object = None,
    baseline_value: float | None = None,
    lower_is_better: bool | None = None,
    selected: bool = False,
    worktime_offset_hours: float = 0.0,
) -> Observation:
    worktime, tokens = _coordinates(telemetry)
    if worktime is not None and worktime_offset_hours:
        worktime += worktime_offset_hours
    objective_value = _number(objective)
    ratio = _ratio(objective_value, baseline_value, lower_is_better)
    return Observation(
        round=round_number,
        worktime_hours=worktime,
        processed_tokens=tokens,
        objective=objective_value,
        ratio=ratio,
        cost_rmb=_cost_rmb(telemetry),
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
    worktime_offsets = _worktime_rebase_offsets(history)
    baseline = None
    if baseline_value is not None:
        baseline = _observation(
            round_number=0,
            telemetry=context.get("baseline_telemetry"),
            objective=baseline_value,
            baseline_value=baseline_value,
            lower_is_better=lower_is_better,
            selected=True,
        )

    candidates: list[Observation] = []
    incumbents: list[Observation] = [baseline] if baseline is not None else []
    rounds: list[int] = []
    incumbent_value = baseline_value

    for index, record in enumerate(history):
        round_number = int(record.get("round", index)) + 1
        rounds.append(round_number)
        offset_hours = (
            worktime_offsets[index]
            if index < len(worktime_offsets)
            else 0.0
        )
        attempts = record.get("candidates") or []
        selected_id = record.get("selected_candidate")
        selected_observation: Observation | None = None

        for attempt in attempts:
            selected = attempt.get("candidate") == selected_id
            metrics = attempt.get("metrics") or {}
            point = _observation(
                round_number=round_number,
                telemetry=attempt.get("telemetry"),
                objective=metrics.get(objective_key),
                baseline_value=baseline_value,
                lower_is_better=lower_is_better,
                selected=selected,
                worktime_offset_hours=offset_hours,
            )
            candidates.append(point)
            if selected:
                selected_observation = point

        accepted = bool(record.get("selected_sha"))
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
                lower_is_better=lower_is_better,
                selected=True,
                worktime_offset_hours=offset_hours,
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
    key = series.objective_key or "Objective"
    if kind == "ratio":
        if series.lower_is_better is True:
            return f"{key} multiple (vs baseline)"
        return f"{key} ratio (vs baseline)"
    return key


def _render_panel(axis, series: PlotSeries, y_kind: str, x_kind: str) -> None:
    candidate_x, candidate_y = _xy(series.candidates, y_kind, x_kind)
    if candidate_x:
        axis.scatter(
            candidate_x,
            candidate_y,
            color="#D88932",
            alpha=0.65,
            s=30,
            label="All candidates",
            zorder=2,
        )

    selected = [point for point in series.candidates if point.selected]
    selected_x, selected_y = _xy(selected, y_kind, x_kind)
    if selected_x:
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
    if (
        y_kind == "objective"
        and series.objective_key == "SPEED_MS"
    ):
        axis.axhline(
            _PAPER_SPEED_MS,
            color="#2E8B57",
            linestyle="--",
            linewidth=1.2,
            label="Paper v1.12.0",
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
    title = _y_label(series, y_kind)
    if y_kind == "objective":
        direction = (
            "lower is better" if series.lower_is_better is True
            else "higher is better" if series.lower_is_better is False
            else "direction not configured"
        )
        title = f"{title} ({direction})"
    elif y_kind == "ratio" and series.lower_is_better is not None:
        # After the direction-aware inversion the ratio is always an improvement
        # multiple, so higher is better either way.
        title = f"{title} (higher is better)"
    axis.set_title(f"{title} vs {_x_label(x_kind)}")
    axis.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
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
    figure, axes = plt.subplots(
        len(_Y_KINDS), len(_X_KINDS), figsize=(16, 9), squeeze=False,
    )
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


def write_progress_png(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> Path | None:
    """Redraw the 2x3 overview image (the only plot refreshed at run time)."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    series = build_series(history, metrics_schema, plot_context)
    overview = run_path / "progress.png"
    if _publish(overview, lambda path: _render_progress_png(series, path)):
        return overview
    return None


def _render_cost_png(series: PlotSeries, output: Path) -> None:
    """Draw cumulative token cost (RMB) vs round as a single-panel detail image."""
    plt = _prepare_pyplot()
    figure, axis = plt.subplots(figsize=(9, 5.5))
    figure.patch.set_facecolor("white")

    # Incumbents carry round-level cumulative cost.
    points = [
        (int(obs.round), obs.cost_rmb)
        for obs in series.incumbents
        if obs.round > 0 and obs.cost_rmb is not None
    ]
    if points:
        rounds = [r for r, _ in points]
        costs = [cost for _, cost in points]
        axis.step(
            rounds,
            costs,
            where="post",
            color="#2856A6",
            linewidth=2.2,
            label="Cumulative cost",
            zorder=3,
        )
        axis.scatter(rounds, costs, color="#B43A3A", marker="D", s=30, zorder=4)
    else:
        axis.text(
            0.5,
            0.5,
            "No data",
            ha="center",
            va="center",
            transform=axis.transAxes,
            color="#6B7280",
        )

    axis.set_xlabel("Round")
    axis.set_ylabel("Cumulative cost (RMB)")
    axis.set_title(
        f"Cumulative token cost vs Round  "
        f"({_COST_MODEL_LABEL}: input/output "
        f"{_COST_RMB_PER_MILLION_TOKENS:g}, cache hit "
        f"{_CACHE_HIT_RMB_PER_MILLION_TOKENS:g} RMB / 1M)"
    )
    axis.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
    if series.rounds:
        axis.set_xticks(_round_ticks(series.rounds))
        axis.set_xlim(-0.5, series.rounds[-1] + 0.5)
    handles, _ = axis.get_legend_handles_labels()
    if handles:
        axis.legend(loc="best", frameon=False, fontsize="small")
    figure.tight_layout()
    try:
        figure.savefig(output, format="png", dpi=140)
    finally:
        plt.close(figure)


def write_cost_png(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> Path | None:
    """Redraw the standalone cumulative-cost image (offline detail)."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    series = build_series(history, metrics_schema, plot_context)
    output = run_path / "progress-cost-vs-round.png"
    if _publish(output, lambda path: _render_cost_png(series, path)):
        return output
    return None

# Reference value for the paper v1.12.0 SPEED_MS, shown as a dashed line on
# objective panels and the dual-axis detail image.
_PAPER_SPEED_MS = 177.7


def _round_cost_points(series: PlotSeries) -> list[tuple[int, float, float]]:
    """Cumulative (round, objective, cost-RMB) tuples for accepted incumbents.

    Only incumbents whose round-level telemetry carries a cumulative
    cumulative cost can be placed on the cost axis.  The objective is the
    incumbent value carried on the same observation."""
    points: list[tuple[int, float, float]] = []
    for obs in series.incumbents:
        if obs.round <= 0 or obs.cost_rmb is None:
            continue
        if obs.objective is None:
            continue
        points.append((int(obs.round), obs.objective, obs.cost_rmb))
    return points


def _render_objective_vs_cost_png(series: PlotSeries, output: Path) -> None:
    """Objective vs cumulative token cost — where the optimization lands for the
    RMB spent.  Plots every candidate (faint), the accepted incumbent step, and
    the selected points highlighted."""
    plt = _prepare_pyplot()
    figure, axis = plt.subplots(figsize=(9, 5.5))
    figure.patch.set_facecolor("white")

    # Candidate scatter: objective vs its own cumulative cost.
    cand_points: list[tuple[float, float]] = []
    for obs in series.candidates:
        if obs.cost_rmb is None or obs.objective is None:
            continue
        cand_points.append((obs.cost_rmb, obs.objective))
    if cand_points:
        axis.scatter(
            [c for c, _ in cand_points],
            [o for _, o in cand_points],
            color="#D88932",
            alpha=0.65,
            s=30,
            label="All candidates",
            zorder=2,
        )

    # Selected candidates highlighted.
    sel_points: list[tuple[float, float]] = []
    for obs in series.candidates:
        if not obs.selected or obs.cost_rmb is None or obs.objective is None:
            continue
        sel_points.append((obs.cost_rmb, obs.objective))
    if sel_points:
        axis.scatter(
            [c for c, _ in sel_points],
            [o for _, o in sel_points],
            color="#B43A3A",
            marker="D",
            s=42,
            label="Selected",
            zorder=4,
        )

    # Accepted incumbent step over cost.
    inc_points = [
        (cost, obj) for _, obj, cost in _round_cost_points(series)
    ]
    if inc_points:
        axis.step(
            [c for c, _ in inc_points],
            [o for _, o in inc_points],
            where="post",
            color="#2856A6",
            linewidth=2.2,
            label="Accepted incumbent",
            zorder=3,
        )

    if series.objective_key == "SPEED_MS":
        axis.axhline(
            _PAPER_SPEED_MS,
            color="#2E8B57",
            linestyle="--",
            linewidth=1.2,
            label="Paper v1.12.0",
            zorder=1,
        )
    if series.baseline is not None and series.baseline.objective is not None:
        axis.axhline(
            series.baseline.objective,
            color="#6B7280",
            linestyle="--",
            linewidth=1.0,
            label="Baseline",
            zorder=1,
        )

    has_data = bool(cand_points) or bool(inc_points)
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

    axis.set_xlabel("Cumulative cost (RMB)")
    axis.set_ylabel(_y_label(series, "objective"))
    direction = (
        "lower is better" if series.lower_is_better is True
        else "higher is better" if series.lower_is_better is False
        else "direction not configured"
    )
    axis.set_title(
        f"{_y_label(series, 'objective')} ({direction}) vs Cumulative cost  "
        f"({_COST_MODEL_LABEL}: input/output "
        f"{_COST_RMB_PER_MILLION_TOKENS:g}, cache hit "
        f"{_CACHE_HIT_RMB_PER_MILLION_TOKENS:g} RMB / 1M)"
    )
    axis.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
    handles, _ = axis.get_legend_handles_labels()
    if handles:
        axis.legend(loc="best", frameon=False, fontsize="small")
    figure.tight_layout()
    try:
        figure.savefig(output, format="png", dpi=140)
    finally:
        plt.close(figure)


def write_objective_vs_cost_png(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> Path | None:
    """Redraw the objective-vs-cost detail image (offline only)."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    series = build_series(history, metrics_schema, plot_context)
    output = run_path / "progress-objective-vs-cost.png"
    if _publish(output, lambda path: _render_objective_vs_cost_png(series, path)):
        return output
    return None


def _render_dual_axis_png(series: PlotSeries, output: Path) -> None:
    """Dual-y-axis detail image for SPEED_MS objectives.

    Left axis: objective (SPEED_MS, log scale).  Right axis: speedup multiple
    (baseline / objective, linear).  Only selected points are drawn, plus dashed
    reference lines for the paper v1.12.0 value and the baseline."""
    plt = _prepare_pyplot()
    figure, left = plt.subplots(figsize=(9, 5.5))
    figure.patch.set_facecolor("white")

    selected = [
        obs for obs in series.candidates
        if obs.selected and obs.objective is not None
    ]
    baseline_value = (
        series.baseline.objective if series.baseline is not None else None
    )

    right = None
    if selected:
        rounds = [int(obs.round) for obs in selected]
        objectives = [obs.objective for obs in selected]
        left.plot(
            rounds,
            objectives,
            color="#B43A3A",
            linewidth=1.4,
            alpha=0.8,
            zorder=3,
        )
        left.scatter(
            rounds,
            objectives,
            color="#B43A3A",
            marker="D",
            s=48,
            label="Selected (SPEED_MS)",
            zorder=4,
        )
        if baseline_value:
            speedups = [baseline_value / o for o in objectives]
            right = left.twinx()
            right.plot(
                rounds,
                speedups,
                color="#2856A6",
                linewidth=1.4,
                alpha=0.8,
                zorder=2,
            )
            right.scatter(
                rounds,
                speedups,
                color="#2856A6",
                marker="o",
                s=36,
                label="Selected (speedup)",
                zorder=3,
            )
            right.set_ylabel("Speedup (× baseline)")
            right.grid(False)
            right.axhline(
                1.0,
                color="#6B7280",
                linestyle="--",
                linewidth=1.0,
                label="Baseline (1×)",
                zorder=1,
            )
    else:
        left.text(
            0.5,
            0.5,
            "No selected points",
            ha="center",
            va="center",
            transform=left.transAxes,
            color="#6B7280",
        )

    left.set_xlabel("Round")
    left.set_ylabel(_y_label(series, "objective"))
    left.set_yscale("log")
    if series.objective_key == "SPEED_MS":
        left.axhline(
            _PAPER_SPEED_MS,
            color="#2E8B57",
            linestyle="--",
            linewidth=1.2,
            label="Paper v1.12.0",
            zorder=1,
        )
    if series.rounds:
        left.set_xticks(_round_ticks(series.rounds))
        left.set_xlim(-0.5, series.rounds[-1] + 0.5)
    left.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
    title = f"{_y_label(series, 'objective')} (log) & speedup vs Round"
    if not selected:
        title = f"{_y_label(series, 'objective')} (log) & speedup — no selected points"
    left.set_title(title)

    # Merge left/right legend entries into one legend inside the plot frame.
    handles, labels = left.get_legend_handles_labels()
    if right is not None:
        r_handles, r_labels = right.get_legend_handles_labels()
        handles += r_handles
        labels += r_labels
    if handles:
        left.legend(
            handles,
            labels,
            loc="best",
            frameon=True,
            framealpha=0.85,
            fontsize="small",
        )
    figure.tight_layout()
    try:
        figure.savefig(output, format="png", dpi=140)
    finally:
        plt.close(figure)



def _render_dual_axis_cost_png(series: PlotSeries, output: Path) -> None:
    """Draw selected objective and speedup against cumulative cost."""
    plt = _prepare_pyplot()
    figure, left = plt.subplots(figsize=(9, 5.5))
    figure.patch.set_facecolor("white")

    selected = [
        obs for obs in series.candidates
        if (
            obs.selected
            and obs.objective is not None
            and obs.cost_rmb is not None
        )
    ]
    baseline_value = (
        series.baseline.objective if series.baseline is not None else None
    )

    right = None
    if selected:
        costs = [obs.cost_rmb for obs in selected]
        objectives = [obs.objective for obs in selected]
        left.plot(costs, objectives, color="#B43A3A", linewidth=1.4,
                  alpha=0.8, zorder=3)
        left.scatter(costs, objectives, color="#B43A3A", marker="D", s=48,
                     label="Selected (SPEED_MS)", zorder=4)
        if baseline_value:
            speedups = [baseline_value / value for value in objectives]
            right = left.twinx()
            right.plot(costs, speedups, color="#2856A6", linewidth=1.4,
                       alpha=0.8, zorder=2)
            right.scatter(costs, speedups, color="#2856A6", marker="o", s=36,
                          label="Selected (speedup)", zorder=3)
            right.set_ylabel("Speedup (× baseline)")
            right.grid(False)
            right.axhline(1.0, color="#6B7280", linestyle="--", linewidth=1.0,
                          label="Baseline (1×)", zorder=1)
    else:
        left.text(0.5, 0.5, "No selected points", ha="center", va="center",
                  transform=left.transAxes, color="#6B7280")

    left.set_xlabel("Cumulative cost (RMB)")
    left.set_ylabel(_y_label(series, "objective"))
    left.set_yscale("log")
    if series.objective_key == "SPEED_MS":
        left.axhline(_PAPER_SPEED_MS, color="#2E8B57", linestyle="--",
                     linewidth=1.2, label="Paper v1.12.0", zorder=1)
    left.grid(True, color="#D9DEE5", linewidth=0.7, alpha=0.75)
    title = f"{_y_label(series, 'objective')} (log) & speedup vs Cumulative cost"
    if not selected:
        title = f"{_y_label(series, 'objective')} (log) & speedup — no selected points"
    left.set_title(title)

    handles, labels = left.get_legend_handles_labels()
    if right is not None:
        right_handles, right_labels = right.get_legend_handles_labels()
        handles += right_handles
        labels += right_labels
    if handles:
        left.legend(handles, labels, loc="best", frameon=True,
                    framealpha=0.85, fontsize="small")
    figure.tight_layout()
    try:
        figure.savefig(output, format="png", dpi=140)
    finally:
        plt.close(figure)


def write_dual_axis_cost_png(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> Path | None:
    """Redraw the objective/speedup-vs-cost detail image."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    series = build_series(history, metrics_schema, plot_context)
    output = run_path / "progress-objective-ratio-vs-cost.png"
    if _publish(output, lambda path: _render_dual_axis_cost_png(series, path)):
        return output
    return None

def write_dual_axis_png(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> Path | None:
    """Redraw the dual-axis objective/speedup detail image (offline only)."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    series = build_series(history, metrics_schema, plot_context)
    output = run_path / "progress-objective-speedup-vs-round.png"
    if _publish(output, lambda path: _render_dual_axis_png(series, path)):
        return output
    return None
