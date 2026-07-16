"""The serial main loop. Scheduling only — the thinking is in the roles.

For each of max_rounds rounds:
  proposer.propose(history) -> proposal
  executor.execute(proposal) -> sha (or None if gate-rejected / empty)
  judger.judge(diff + eval)  -> {score, feedback}
  store.append(...)
  if sha: parent_sha = sha   # chain advances; else chain stays put

No early stop, no batch, no parallel. The orchestrator never decides whether a
round is "good" — it just records the judger's score and feeds feedback forward.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .agent import Agent, AgentError
from . import config as config_mod
from . import executor as executor_mod
from . import judger as judger_mod
from . import proposer as proposer_mod
from .store import Store
from .workspace import Workspace


def run(config_path: str | Path, run_dir: str | Path) -> dict:
    """Run the full loop. Returns a summary dict."""
    cfg = config_mod.load(config_path)
    run_dir_path = Path(run_dir)
    run_dir_path.mkdir(parents=True, exist_ok=True)

    agent = Agent(
        command="claude",
        timeout_seconds=1800,
    )
    workspace = Workspace(
        run_dir=run_dir_path,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    store = Store(run_dir_path)

    print(f"[{stamp()}] setting up working repo (clone --local from {cfg['repo_path']})", flush=True)
    workspace.setup()
    parent_sha = workspace.baseline_sha()
    print(f"[{stamp()}] baseline sha: {parent_sha}", flush=True)

    for round_id in range(cfg["max_rounds"]):
        print(f"\n[{stamp()}] === round {round_id + 1}/{cfg['max_rounds']} ===", flush=True)

        # 1. proposer — reads the ORIGINAL source repo (read-only) to propose a
        #    direction. It never touches the working repo; the executor edits there.
        try:
            proposal = proposer_mod.propose(
                agent, goal=cfg["goal"], editable=cfg["editable_paths"],
                frozen=cfg["frozen_paths"], history=store.history(),
                cwd=Path(cfg["repo_path"]),
            )
        except AgentError as exc:
            print(f"[{stamp()}] proposer failed: {exc}", flush=True)
            _record_failure(store, round_id, "", "proposer failed: " + str(exc)[:200])
            continue
        print(f"[{stamp()}] proposal: {proposal[:150]}", flush=True)

        # 2. executor (+ gate + harness commit)
        worktree = workspace.add_worktree(round_id, parent_sha)
        try:
            result = executor_mod.execute(
                agent, proposal=proposal, goal=cfg["goal"],
                editable=cfg["editable_paths"], frozen=cfg["frozen_paths"],
                workspace=workspace, worktree=worktree, round_id=round_id,
            )
        except AgentError as exc:
            print(f"[{stamp()}] executor failed: {exc}", flush=True)
            workspace.remove_worktree(round_id)
            _record_failure(store, round_id, proposal, "executor failed: " + str(exc)[:200])
            continue
        finally:
            workspace.remove_worktree(round_id)

        if result.sha:
            print(f"[{stamp()}] committed: {result.sha} ({len(result.changed_paths)} files)", flush=True)
        else:
            print(f"[{stamp()}] no commit: {result.reason}", flush=True)

        # 3. judger (diff + eval, harness-computed)
        try:
            judgment = judger_mod.judge(
                agent, goal=cfg["goal"], proposal=proposal, sha=result.sha,
                reason=result.reason, parent_sha=parent_sha, workspace=workspace,
                eval_commands=cfg["eval_commands"], cwd=workspace.repo,
            )
        except AgentError as exc:
            print(f"[{stamp()}] judger failed: {exc}", flush=True)
            _record_failure(store, round_id, proposal, "judger failed: " + str(exc)[:200], result.sha)
            if result.sha:
                parent_sha = result.sha
            continue

        print(f"[{stamp()}] score={judgment.score:.2f}  feedback: {judgment.feedback[:150]}", flush=True)

        # 4. record + advance chain
        store.append(round_id, proposal, result.sha, judgment.score, judgment.feedback)
        if result.sha:
            parent_sha = result.sha

    report = store.write_final_report(cfg["goal"])
    print(f"\n[{stamp()}] done. best={store.best_sha} (score {store.best_score:.2f})", flush=True)
    print(f"[{stamp()}] report: {report}", flush=True)
    print(f"[{stamp()}] working repo (for tracing): {workspace.repo}", flush=True)
    return {
        "best_sha": store.best_sha,
        "best_score": store.best_score,
        "rounds": len(store.history()),
        "run_dir": str(run_dir_path),
        "repo": str(workspace.repo),
    }


def _record_failure(store: Store, round_id: int, proposal: str, reason: str,
                    parent_sha: str, sha: str | None = None) -> None:
    """Record a round where a role crashed, so history stays complete."""
    store.append(round_id, proposal, sha, 0.0, f"[loop failure] {reason}")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
