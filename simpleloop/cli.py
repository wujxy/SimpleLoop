"""SimpleLoop CLI entry point.

  simpleloop run --config task.yaml --run-dir ./run-001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import loop
from . import config as config_mod
from . import memory


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
             "round's sha. Baseline eval is re-run for the judger's vs-baseline axis.",
    )

    validate = sub.add_parser("validate", help="Validate a config without running.")
    validate.add_argument("--config", required=True, help="Task config (YAML/JSON).")

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
        return

    if args.command == "run":
        try:
            summary = loop.run(args.config, args.run_dir,
                               proposals=args.proposals,
                               continue_run=args.continue_run)
        except config_mod.ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        except ValueError as exc:
            # bad --proposals file (wrong shape / empty entry) or a static batch
            # that is empty — surface it clearly, do not start a half-run.
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"\nBest: {summary['best_sha']} (score {summary['best_score']:.2f}) "
              f"over {summary['rounds']} rounds")
        print(f"Working repo: {summary['repo']}")
        return


if __name__ == "__main__":
    main()
