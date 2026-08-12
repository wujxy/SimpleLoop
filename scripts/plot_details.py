#!/usr/bin/env python3
"""Draw a run's six single-panel detail images offline.

The loop only maintains the 2x3 overview (progress.png); run this script to get
the per-panel detail PNGs:

    python scripts/plot_details.py --config task.yaml --run-dir ./run-001
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from simpleloop import config as config_mod
from simpleloop.harness import memory
from simpleloop.reporting import telemetry as telemetry_mod
from simpleloop.reporting import plot as plot_mod
from simpleloop.reporting.plot import (
    PlotSeries,
    _prepare_pyplot,
    _round_ticks,
    _y_label,
    _PAPER_SPEED_MS,
    _COST_RMB_PER_MILLION_TOKENS,
    _CACHE_HIT_RMB_PER_MILLION_TOKENS,
)

_Y_SLUGS = {"objective": "objective", "ratio": "objective-ratio"}
_COST_MODEL_LABEL = "glm-5.2"


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



def _render_detail(
    series: plot_mod.PlotSeries,
    y_kind: str,
    x_kind: str,
    output: Path,
) -> None:
    plt = plot_mod._prepare_pyplot()
    figure, axis = plt.subplots(figsize=(9, 5.5))
    figure.patch.set_facecolor("white")
    plot_mod._render_panel(axis, series, y_kind, x_kind)
    figure.tight_layout()
    try:
        figure.savefig(output, format="png", dpi=140)
    finally:
        plt.close(figure)


def write_detail_pngs(
    run_dir: str | Path,
    history: list[dict],
    metrics_schema: dict | None,
    plot_context: dict | None = None,
) -> list[Path]:
    """Redraw the six detail images; each is published independently so one
    failure doesn't block the rest."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    series = plot_mod.build_series(history, metrics_schema, plot_context)
    published: list[Path] = []
    for y_kind in plot_mod._Y_KINDS:
        for x_kind in plot_mod._X_KINDS:
            output = run_path / (
                f"progress-{_Y_SLUGS[y_kind]}-vs-{x_kind}.png"
            )
            if plot_mod._publish(
                output,
                lambda path, y=y_kind, x=x_kind: _render_detail(
                    series, y, x, path
                ),
            ):
                published.append(output)
    cost_output = run_path / "progress-cost-vs-round.png"
    if plot_mod._publish(
        cost_output,
        lambda path: _render_cost_png(series, path),
    ):
        published.append(cost_output)
    obj_cost_output = run_path / "progress-objective-vs-cost.png"
    if plot_mod._publish(
        obj_cost_output,
        lambda path: _render_objective_vs_cost_png(series, path),
    ):
        published.append(obj_cost_output)
    dual_output = run_path / "progress-objective-speedup-vs-round.png"
    if plot_mod._publish(
        dual_output,
        lambda path: _render_dual_axis_png(series, path),
    ):
        published.append(dual_output)
    cost_dual_output = run_path / "progress-objective-ratio-vs-cost.png"
    if plot_mod._publish(
        cost_dual_output,
        lambda path: _render_dual_axis_cost_png(series, path),
    ):
        published.append(cost_dual_output)
    return published


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Redraw a run's six detail progress images offline.")
    parser.add_argument(
        "--config", required=True,
        help="The task config the run used (source of eval.metrics).")
    parser.add_argument(
        "--run-dir", required=True,
        help="The run directory holding history.jsonl / telemetry.json.")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    run_dir = Path(args.run_dir).expanduser().resolve()
    history_path = run_dir / "history.jsonl"
    if not history_path.exists():
        print(f"Error: no history.jsonl at {run_dir}", file=sys.stderr)
        raise SystemExit(1)
    try:
        history = memory.read_history(history_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    plot_context = telemetry_mod.load_plot_context(run_dir)
    outputs = write_detail_pngs(run_dir, history, cfg["metrics"], plot_context)
    if not outputs:
        print("Error: no image could be written", file=sys.stderr)
        raise SystemExit(1)
    for path in outputs:
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
