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
                f"progress-{plot_mod._Y_SLUGS[y_kind]}-vs-{x_kind}.png"
            )
            if plot_mod._publish(
                output,
                lambda path, y=y_kind, x=x_kind: _render_detail(
                    series, y, x, path
                ),
            ):
                published.append(output)
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
