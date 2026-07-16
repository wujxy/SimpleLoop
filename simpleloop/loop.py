"""The serial main loop. Scheduling only — the thinking is in the roles.

For each of max_rounds rounds:
  proposer.propose(history) -> proposal   (or a pre-supplied proposal; see below)
  executor.execute(proposal) -> sha (or None if gate-rejected / empty)
  judger.judge(diff + eval)  -> {score, feedback}
  store.append(...)
  if sha: parent_sha = sha   # chain advances; else chain stays put

No early stop, no batch, no parallel. The orchestrator never decides whether a
round is "good" — it just records the judger's score and feeds feedback forward.

Static-proposal mode: pass `proposals=[str, ...]` (or a path to a YAML/JSON
file of such a list) to SKIP the claude proposer and drive the loop from a
fixed batch of directions you prepared. Round i uses proposals[i]; the number
of rounds is len(proposals) (max_rounds is ignored in this mode). The judger
still runs and scores each, but its feedback no longer chooses the next proposal.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml

from .agent import Agent, AgentError
from . import config as config_mod
from . import executor as executor_mod
from . import judger as judger_mod
from . import proposer as proposer_mod
from .store import Store
from .workspace import Workspace


def run(config_path: str | Path, run_dir: str | Path,
        proposals: str | Path | list[str] | None = None) -> dict:
    """Run the full loop. Returns a summary dict.

    If `proposals` is given (a list of strings, or a path to a YAML/JSON file
    holding one), the claude proposer is skipped and round i uses proposals[i].
    """
    cfg = config_mod.load(config_path)
    # Resolve to absolute now: every derived path (repo, worktrees) must be
    # absolute so git and the agent subprocess (Popen cwd=) agree on location.
    # A relative run_dir otherwise makes git create worktrees relative to the
    # repo's cwd while Popen resolves them relative to the loop's cwd -> mismatch.
    run_dir_path = Path(run_dir).resolve()
    run_dir_path.mkdir(parents=True, exist_ok=True)

    static_proposals = _load_proposals(proposals)
    if static_proposals is not None:
        n_rounds = len(static_proposals)
        print(f"[{stamp()}] STATIC-PROPOSAL mode: {n_rounds} round(s) from the "
              f"supplied batch (config max_rounds={cfg['max_rounds']} ignored)", flush=True)
    else:
        n_rounds = cfg["max_rounds"]

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

    # Baseline eval: run the eval commands once on the unoptimized baseline
    # commit so every round's judger has a "vs baseline" axis. Without it the
    # judger only sees each round's absolute number and can't tell a real
    # optimization from a round that merely beats a weak prior round. Best-effort:
    # a baseline-eval failure does NOT kill the run (the judger falls back to
    # prior-round-only comparison). Skipped entirely when no eval is configured.
    baseline_eval_block = _eval_baseline(workspace, cfg, parent_sha)
    # round 0's "prior round" is the baseline. Updated to each round's eval_block
    # after that round is recorded.
    prior_eval_block = baseline_eval_block

    for round_id in range(n_rounds):
        print(f"\n[{stamp()}] === round {round_id + 1}/{n_rounds} ===", flush=True)

        # 1. proposer — reads the ORIGINAL source repo (read-only) to propose a
        #    direction. It never touches the working repo; the executor edits there.
        #    In static-proposal mode this step is skipped: the proposal is taken
        #    verbatim from the supplied batch (history is still recorded, but its
        #    feedback no longer picks the next direction).
        if static_proposals is not None:
            proposal = static_proposals[round_id]
            print(f"[{stamp()}] proposal (static): {proposal[:150]}", flush=True)
        else:
            try:
                proposal = proposer_mod.propose(
                    agent, goal=cfg["goal"], editable=cfg["editable_paths"],
                    frozen=cfg["frozen_paths"], history=store.history(),
                    cwd=Path(cfg["repo_path"]),
                )
            except (AgentError, ValueError) as exc:
                # AgentError = claude call failed/unparseable; ValueError = empty
                # 'proposal'. Either way record and skip the round, keep going.
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

        if result.sha:
            print(f"[{stamp()}] committed: {result.sha} ({len(result.changed_paths)} files)", flush=True)
        else:
            print(f"[{stamp()}] no commit: {result.reason}", flush=True)

        # 3. eval + 4. judger — both run while the worktree still exists.
        #    eval runs in the worktree (the real committed tree; the bare per-run
        #    repo clone has no working tree). The judger ALSO runs in the worktree
        #    so it can cat/grep the actual source and re-run a command to verify a
        #    claim. The worktree is removed only after the judger returns.
        eval_block = ""
        if result.sha and cfg["eval_commands"]:
            try:
                eval_block = judger_mod.run_eval(cfg["eval_commands"], cwd=worktree)
            except Exception as exc:  # timeout or subprocess error
                eval_block = f"(eval failed to run: {exc})"
                print(f"[{stamp()}] eval error: {exc}", flush=True)

        try:
            judgment = judger_mod.judge(
                agent, goal=cfg["goal"], proposal=proposal, sha=result.sha,
                reason=result.reason, parent_sha=parent_sha, workspace=workspace,
                eval_block=eval_block, cwd=worktree,
                prior_eval_block=prior_eval_block,
                baseline_eval_block=baseline_eval_block,
            )
        except (AgentError, ValueError) as exc:
            # AgentError = claude call failed; ValueError = judger returned a
            # malformed score/feedback. Either way: record, advance the chain if
            # a commit exists, and keep going — one bad judgment must not kill a
            # 10-round run.
            print(f"[{stamp()}] judger failed: {exc}", flush=True)
            _record_failure(store, round_id, proposal, "judger failed: " + str(exc)[:200], result.sha,
                            eval_block)
            if result.sha:
                parent_sha = result.sha
            continue
        finally:
            # the worktree must not leak, whatever the judger raised.
            workspace.remove_worktree(round_id)

        print(f"[{stamp()}] score={judgment.score:.2f}  feedback: {judgment.feedback[:150]}", flush=True)

        # 4. record + advance chain. eval_block is stored so the NEXT round's
        #    judger gets this round's result as its "prior round" axis.
        store.append(round_id, proposal, result.sha, judgment.score, judgment.feedback, eval_block)
        prior_eval_block = eval_block or prior_eval_block
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
                    sha: str | None = None, eval_block: str = "") -> None:
    """Record a round where a role crashed, so history stays complete."""
    store.append(round_id, proposal, sha, 0.0, f"[loop failure] {reason}", eval_block)


def _eval_baseline(workspace: Workspace, cfg: dict, baseline_sha: str) -> str | None:
    """Run the eval commands once on the unoptimized baseline commit.

    Returns the eval output block (the judger's "vs baseline" axis), or None if
    no eval is configured or the baseline eval failed. Best-effort: failures are
    logged and swallowed — the run continues with prior-round-only comparison.
    Uses a throwaway worktree on the baseline SHA (the bare per-run clone has no
    working tree, same reason the per-round eval uses a worktree).
    """
    if not cfg["eval_commands"]:
        return None
    print(f"[{stamp()}] running baseline eval (on {baseline_sha[:10]}) for the judger's "
          f"vs-baseline axis...", flush=True)
    wt = None
    try:
        wt = workspace.add_worktree("baseline", baseline_sha)
        block = judger_mod.run_eval(cfg["eval_commands"], cwd=wt)
        print(f"[{stamp()}] baseline eval done.", flush=True)
        return block
    except Exception as exc:  # worktree add or eval failure
        print(f"[{stamp()}] baseline eval failed (judger will use prior-round-only "
              f"comparison): {exc}", flush=True)
        return None
    finally:
        if wt is not None:
            workspace.remove_worktree("baseline")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_proposals(proposals: str | Path | list[str] | None) -> list[str] | None:
    """Resolve the `proposals` argument to a list of non-empty strings, or None.

    - None -> None (normal loop: claude proposer each round).
    - list[str] -> used directly (the Python-call path: loop.run(proposals=[...])).
    - str/Path -> a YAML/JSON file; the document must be a list of strings, or an
      object with a "proposals" key holding such a list. YAML block scalars (``- |``)
      are the convenient way to write multi-line directions.

    Every entry must be a non-empty string — an empty proposal would waste a whole
    executor+judger round, so fail fast here instead.
    """
    if proposals is None:
        return None
    if isinstance(proposals, list):
        raw = proposals
    else:
        path = Path(proposals).expanduser()
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            doc = json.loads(text)
        else:  # .yaml / .yml / anything else -> try YAML
            doc = yaml.safe_load(text)
        if isinstance(doc, dict) and isinstance(doc.get("proposals"), list):
            raw = doc["proposals"]
        elif isinstance(doc, list):
            raw = doc
        else:
            raise ValueError(
                f"proposals file {path}: expected a list of strings (or "
                f"{{'proposals': [...]}}), got {type(doc).__name__}"
            )
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"proposals[{i}]: must be a non-empty string, got {item!r}")
        out.append(item.strip())
    if not out:
        raise ValueError("proposals: file/list is empty — nothing to run")
    return out
