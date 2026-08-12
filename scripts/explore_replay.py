#!/usr/bin/env python3
"""Replay Explore search-health over a finished run, round by round.

For each round R the proposer would have woken at, this script reconstructs
the Ledger + Finding state visible up to R-1 and prints when each Explore
policy signal (per-finding, family, global) would have first fired — and
whether ``challenge_required`` would have been set.

Usage:
    python scripts/explore_replay.py <run_dir>
    python scripts/explore_replay.py runs/test01
    python scripts/explore_replay.py <run_dir> --objective-key SPEED_MS \
        --lower-is-better

Objective resolution order: ``--objective-key`` flag wins; else read
``<run_dir>/config.resolved.json`` at ``metrics.objective.{key,lower_is_better}``.

Caveat: the Finding archive is the *latest* state of each finding (tags rarely
change), so this is a faithful but not bit-exact historical replay of family
detection.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from a source checkout without install.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from simpleloop.explore import analyze_explore_health  # noqa: E402
from simpleloop.harness.memory import read_history  # noqa: E402
from simpleloop.memory.experiment_index import build_experiments  # noqa: E402
from simpleloop.memory.finding_store import FindingStore  # noqa: E402


def _resolve_objective(
    run_dir: Path, args: argparse.Namespace,
) -> tuple[str | None, bool]:
    if args.objective_key:
        return args.objective_key, not args.higher_is_better
    config_path = args.config or (run_dir / "config.resolved.json")
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            obj = (cfg.get("metrics") or {}).get("objective") or {}
            key = obj.get("key")
            if key:
                return key, bool(obj.get("lower_is_better", True))
        except (json.JSONDecodeError, OSError):
            pass
    return None, True


def _active_signals(report) -> list[str]:
    out: list[str] = []
    for fh in report.findings:
        for s in fh.policy_signals:
            if s.active:
                out.append(f"{fh.finding_id}:{s.name}")
    for fam in report.families:
        for s in fam.policy_signals:
            if s.active:
                out.append(f"family:{s.name}({fam.code_region})")
    if report.global_health is not None:
        for s in report.global_health.policy_signals:
            if s.active:
                out.append(f"global:{s.name}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path, help="Finished run directory.")
    parser.add_argument("--objective-key", default=None,
                        help="Override the objective metric key.")
    parser.add_argument("--higher-is-better", action="store_true",
                        help="Set objective direction (default lower is better).")
    parser.add_argument("--config", type=Path, default=None,
                        help="Config JSON for objective resolution.")
    args = parser.parse_args()

    run_dir: Path = args.run_dir
    history_path = run_dir / "history.jsonl"
    if not history_path.exists():
        print(f"error: {history_path} not found", file=sys.stderr)
        return 2

    history = read_history(history_path)
    experiments = build_experiments(history)
    findings = FindingStore(run_dir).load_all()

    objective_key, lower_is_better = _resolve_objective(run_dir, args)
    if not objective_key:
        print(
            "error: could not resolve objective key (pass --objective-key or a "
            "config.resolved.json with metrics.objective.key)",
            file=sys.stderr,
        )
        return 2

    if not experiments:
        print("(no experiments in run)")
        return 0

    min_round = min(e.round for e in experiments)
    max_round = max(e.round for e in experiments)

    print(f"run_dir: {run_dir}")
    print(f"objective: {objective_key} (lower_is_better={lower_is_better})")
    print(f"findings: {len(findings)}  experiments: {len(experiments)}  "
          f"rounds: {min_round}..{max_round}")
    print(
        "note: findings are latest-state, so family detection is faithful but "
        "not bit-exact per round."
    )
    print()

    header = (
        f"{'wake@':>6}  {'exps':>4}  {'first':>5}  "
        f"{'consec_no_imp_rnds':>18}  {'challenge':>9}  active signals"
    )
    print(header)
    print("-" * len(header))

    first_fire: dict[str, int] = {}
    for wake_round in range(min_round + 1, max_round + 2):
        visible = [e for e in experiments if e.round < wake_round]
        if not visible:
            continue
        report = analyze_explore_health(
            findings, visible, current_round=wake_round,
            objective_key=objective_key, lower_is_better=lower_is_better,
        )
        active = _active_signals(report)
        consec = (report.global_health.consecutive_no_improve_rounds
                  if report.global_health is not None else "-")
        challenge = "YES" if report.challenge_required else "-"
        sig_str = ", ".join(active) if active else "(none)"
        print(
            f"r{wake_round:>5}  {len(visible):>4}  "
            f"{str(report.first_round):>5}  {str(consec):>18}  "
            f"{challenge:>9}  {sig_str}"
        )
        for sig in active:
            first_fire.setdefault(sig, wake_round)

    if first_fire:
        print()
        print("first round each signal fired:")
        for sig in sorted(first_fire, key=lambda s: (first_fire[s], s)):
            print(f"  r{first_fire[s]}: {sig}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
