"""SimpleLoop CLI entry point.

  simpleloop run --config task.yaml --run-dir ./run-001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import app
from . import config as config_mod
from .harness import memory
from .container.image import ImageBuildError, build_image
from .world import SandboxPreflightError
from .initialize import InitError, initialize
from .reporting import plot as plot_mod
from .reporting import telemetry as telemetry_mod


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SimpleLoop: minimal serial LLM optimization loop.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the loop.")
    run.add_argument("--config", required=True, help="Task config (YAML/JSON).")
    run.add_argument("--run-dir", required=True, help="Directory for this run's repo + artifacts.")
    run.add_argument(
        "--proposals",
        help="YAML/JSON file holding a list of proposal strings. When given, the "
             "claude proposer is SKIPPED and round i uses proposals[i] (controlled-"
             "experiment mode); runs for len(proposals) rounds, ignoring max_rounds.",
    )
    run.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        help="Resume an existing run-dir. Rounds already recorded in history.jsonl "
             "are skipped; the loop runs from the next round up to loop.max_rounds "
             "(which becomes the TARGET TOTAL round count -- bump it in the config "
             "before continuing). The commit chain resumes from the last recorded "
             "round's sha. Baseline evaluation is re-run for comparison.",
    )

    validate = sub.add_parser("validate", help="Validate a config without running.")
    validate.add_argument("--config", required=True, help="Task config (YAML/JSON).")

    init_parser = sub.add_parser(
        "init",
        help="Prepare a task's Git repository and Apptainer image.",
    )
    init_parser.add_argument(
        "--config",
        required=True,
        help="Task config (YAML/JSON).",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild the configured Apptainer image even when it exists.",
    )

    image_parser = sub.add_parser(
        "image",
        help="Build Apptainer images.",
    )
    image_sub = image_parser.add_subparsers(
        dest="image_command",
        required=True,
    )
    image_build = image_sub.add_parser(
        "build",
        help="Build a SIF from a definition file.",
    )
    image_build.add_argument("definition")
    image_build.add_argument("--output")
    image_build.add_argument(
        "--force",
        action="store_true",
        help="Explicitly allow Apptainer to overwrite an existing SIF.",
    )

    plot_parser = sub.add_parser(
        "plot",
            help="Redraw a run's 2x3 overview image offline. For the six "
             "single-panel detail images use scripts/plot_details.py.",
    )
    plot_parser.add_argument(
        "--config",
        help="The task config the run used (source of eval.metrics). Optional: "
             "defaults to the config.resolved.json snapshot in the run dir.",
    )
    plot_parser.add_argument(
        "--run-dir", required=True,
        help="The run directory holding history.jsonl / telemetry.json.",
    )

    export_parser = sub.add_parser(
        "export",
        help="Export a run's winning commit: diff + bundle + EXPORT.md into "
             "run_dir/export (and optionally a branch in the source repo).",
    )
    export_parser.add_argument(
        "--run-dir", required=True,
        help="The run directory holding repo/, history.jsonl and "
             "config.resolved.json.",
    )
    export_parser.add_argument(
        "--what", choices=("best", "head"), default="best",
        help="best = the harness-selected best candidate (default); "
                 "head = the end of the cumulative selected chain.",
    )
    export_parser.add_argument(
        "--to-branch",
        help="Also push the exported sha into the SOURCE repo as this new "
             "branch (refused if the branch already exists).",
    )

    memory_parser = sub.add_parser(
        "memory", help="Inspect current-run Search Memory."
    )
    memory_sub = memory_parser.add_subparsers(
        dest="memory_command", required=True
    )
    memory_show = memory_sub.add_parser(
        "show",
        help="Show one historical candidate by r<round>c<candidate> ref.",
    )
    memory_show.add_argument("ref")
    memory_show.add_argument("--run-dir")

    args = parser.parse_args(argv)

    if args.command == "init":
        try:
            result = initialize(args.config, force=args.force)
        except (config_mod.ConfigError, InitError) as exc:
            print(f"Init error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"Initializing: {args.config}")
        print(f"  Git source: {result.repo_status} ({result.repo_path})")
        print(
            f"  Apptainer image: {result.image_status} "
            f"({result.image_path})"
        )
        print("  Configuration: valid")
        print("Ready to run.")
        return

    if args.command == "image":
        try:
            output = build_image(
                args.definition,
                args.output,
                force=args.force,
            )
        except ImageBuildError as exc:
            print(f"Image build error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"Built image: {output}")
        return

    if args.command == "export":
        from .harness import export as export_mod
        try:
            info = export_mod.export_run(
                args.run_dir, what=args.what, to_branch=args.to_branch)
        except (export_mod.ExportError, config_mod.ConfigError, ValueError) as exc:
            print(f"Export error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        obj = info["objective_key"]
        print(f"Exported {info['what']}: {info['sha']}")
        if info["objective"] is not None:
            baseline = (f" (baseline: {info['baseline_objective']})"
                        if info["baseline_objective"] is not None else "")
            print(f"  {obj} = {info['objective']}{baseline}")
        print(f"  diff:   {info['diff']}")
        print(f"  bundle: {info['bundle']}")
        print(f"  notes:  {info['readme']}")
        if info["branch"]:
            print(f"  branch: {info['branch']} -> {info['source_repo']}")
        return

    if args.command == "plot":
        run_dir = Path(args.run_dir).expanduser().resolve()
        try:
            cfg = (config_mod.load(args.config) if args.config
                   else config_mod.load_resolved(run_dir))
        except config_mod.ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            raise SystemExit(1)
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
        overview = plot_mod.write_progress_png(
            run_dir, history, cfg["metrics"], plot_context)
        if overview is None:
            print("Error: no image could be written", file=sys.stderr)
            raise SystemExit(1)
        print(f"Wrote {overview}")
        return

    if args.command == "memory":
        if args.run_dir:
            run_dir = Path(args.run_dir).expanduser().resolve()
        else:
            cwd = Path.cwd()
            run_dir = cwd if (cwd / "history.jsonl").exists() else cwd.parent
        history_path = run_dir / "history.jsonl"
        if not history_path.exists():
            print(
                f"Error: no history.jsonl for current run at {run_dir}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        try:
            episode = memory.resolve_episode(
                memory.read_history(history_path),
                args.ref,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(episode, ensure_ascii=False, indent=2))
        return

    if args.command == "validate":
        try:
            cfg = config_mod.load(args.config)
        except config_mod.ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"Valid config: {args.config}")
        print(f"  goal: {cfg['goal']}")
        print(f"  max_rounds: {cfg['max_rounds']}")
        print(f"  candidates_per_round: {cfg['candidates_per_round']}")
        print(f"  max_workers: {cfg['max_workers']}")
        print(f"  eval commands: {len(cfg['eval_commands'])}")
        print(f"  repo: {cfg['repo_path']} @ {cfg['baseline_ref']}")
        print(f"  runtime image: {cfg['runtime_image']}")
        binds = ", ".join(cfg["runtime_binds"]) or "(none)"
        print(f"  runtime binds: {binds}")
        return

    if args.command == "run":
        try:
            cfg = config_mod.load(args.config)
            summary = app.run(
                args.config, args.run_dir, proposals=args.proposals,
                continue_run=args.continue_run,
            )
        except config_mod.ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        except app.RunLockError as exc:
            print(f"Lock error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        except SandboxPreflightError as exc:
            print(f"Runtime error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        except app.BaselineAcceptanceError as exc:
            print(f"Baseline error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        except ValueError as exc:
            # bad --proposals file or empty static batch — do not start a half-run.
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        objective = summary.get("best_objective")
        objective_key = summary.get("objective_key")
        measured = (
            f" ({objective_key} {objective})"
            if objective_key and objective is not None else ""
        )
        print(
            f"\nBest: {summary['best_sha']}{measured} "
            f"over {summary['rounds']} rounds"
        )
        print(f"Working repo: {summary['repo']}")
        return


if __name__ == "__main__":
    main()
