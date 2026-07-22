"""The serial main loop. Scheduling only — the thinking is in the roles.

For each of max_rounds rounds:
  proposer.propose(history) -> proposal   (or a pre-supplied proposal; see below)
  executor.execute(proposal) -> sha (or None if gate-rejected / empty)
  judger.judge(diff + eval)  -> {score, feedback}
  store.append(...)
  if candidate passes every configured hard gate: parent_sha = candidate_sha
  else: parent_sha stays put (the rejected candidate remains in history)

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml

from .agent import Agent, AgentError
from . import config as config_mod
from . import executor as executor_mod
from . import judger as judger_mod
from . import plot as plot_mod
from . import proposer as proposer_mod
from . import views
from .store import Store
from .workspace import Workspace


def run(config_path: str | Path, run_dir: str | Path,
        proposals: str | Path | list[str] | None = None,
        continue_run: bool = False) -> dict:
    """Run the full loop. Returns a summary dict.

    If `proposals` is given (a list of strings, or a path to a YAML/JSON file
    holding one), the claude proposer is skipped and round i uses proposals[i].

    If `continue_run` is True, resume an existing run-dir: rounds already in
    history.jsonl are skipped, the commit chain resumes from the last accepted
    round's sha, and loop.max_rounds is treated as the target TOTAL round count
    (so bump it in the config before continuing). Baseline eval is re-run for
    the judger's vs-baseline axis (cheap relative to the rounds being added).
    """
    cfg = config_mod.load(config_path)
    # Resolve to absolute now: every derived path (repo, worktrees) must be
    # absolute so git and the agent subprocess (Popen cwd=) agree on location.
    # A relative run_dir otherwise makes git create worktrees relative to the
    # repo's cwd while Popen resolves them relative to the loop's cwd -> mismatch.
    run_dir_path = Path(run_dir).resolve()
    run_dir_path.mkdir(parents=True, exist_ok=True)

    static_proposals = _load_proposals(proposals)
    if static_proposals is not None and continue_run:
        raise ValueError("--continue cannot be combined with --proposals: continue "
                         "resumes a claude-proposer run from its history, but "
                         "--proposals drives rounds from a fixed batch.")
    if static_proposals is not None:
        n_rounds = len(static_proposals)
        print(f"[{stamp()}] STATIC-PROPOSAL mode: {n_rounds} round(s) from the "
              f"supplied batch (config max_rounds={cfg['max_rounds']} ignored)", flush=True)
    else:
        n_rounds = cfg["max_rounds"]

    # Three role-scoped agents, separated by the tools each role NEEDS so a role
    # can't do a job it isn't supposed to (reward-hacking / cross-run cheating):
    #   - proposer: reads the source repo to propose a direction. Never edits —
    #     giving it Edit/Write is pure attack surface (it could rewrite the
    #     baseline). Read + Bash only.
    #   - executor: edits the worktree and runs build/test to verify. Needs the
    #     full set; its WRITE risk is bounded by gate.check_diff (frozen/editable).
    #   - judger: reads the diff + re-runs eval to verify claims. Never edits — a
    #     judger that can write could rewrite eval/reference to make itself pass.
    #     Read + Bash only.
    # All three share the same 1h timeout ceiling (configurable per-task via
    # loop.agent_timeout_seconds): a proposal can be a large systematic change
    # spanning many call sites, and the executor must finish the WHOLE proposal in
    # one round (no half-work).
    timeout = cfg.get("agent_timeout_seconds", 3600)
    proposer_agent = Agent(command="claude", timeout_seconds=timeout,
                           allowed_tools="Read,Bash")
    executor_agent = Agent(command="claude", timeout_seconds=timeout,
                           allowed_tools="Read,Edit,Write,Bash")
    judger_agent = Agent(command="claude", timeout_seconds=timeout,
                         allowed_tools="Read,Bash")
    workspace = Workspace(
        run_dir=run_dir_path,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    store = Store(run_dir_path, metrics_schema=cfg.get("metrics"))

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
    # Returns both the raw text (kept for the record) and the parsed metrics
    # (the authoritative baseline numbers the judger's FACTS block cites).
    metrics_schema = cfg.get("metrics")
    # Pre-render the gates' list lines once for the run: the same bullet lines
    # go into both the proposer's (reference) and executor's (acceptance) prompt,
    # each prefixed by its own fixed framing sentence in that role's prompt. Empty
    # when no gate declares a description (degrades to today: no gate list shown).
    gate_lines = views.gate_block(metrics_schema)

    # ---- continue mode: resume from existing history ----
    # Rounds already in history.jsonl are skipped; the commit chain resumes from
    # the last accepted round's sha. max_rounds becomes the target TOTAL round
    # count (caller bumps it in the config before continuing), so the loop runs
    # range(start_round, n_rounds). Baseline eval is re-run for the judger's
    # vs-baseline axis; the prior-round axis is rebuilt from the last accepted
    # round's metrics so the resumed round 0's judger sees a real prior.
    start_round = 0
    if continue_run:
        done = store.history()
        if not done:
            raise ValueError(
                f"--continue: run-dir {run_dir_path} has no history.jsonl rounds; "
                "drop --continue and start a fresh run."
            )
        start_round = len(done)
        if start_round >= n_rounds:
            print(f"[{stamp()}] --continue: {start_round} round(s) already recorded, "
                  f"max_rounds={n_rounds} -- nothing to do. Bump loop.max_rounds in "
                  f"the config to add more rounds.", flush=True)
            _refresh_progress_plot(store)
            store.write_final_report(cfg["goal"])
            return _summary(store, workspace, run_dir_path)
        parent_sha, last_accepted = _resume_chain(
            done, workspace.baseline_sha(), metrics_schema)
        prior_metrics = (last_accepted or {}).get("metrics") or {}
        prior_eval_block = (last_accepted or {}).get("eval_block") or ""
        print(f"[{stamp()}] --continue: resuming from round {start_round + 1} "
              f"(parent_sha={parent_sha[:10]}, {start_round} round(s) already done)",
              flush=True)
        # baseline eval still runs (judger's vs-baseline axis); its metrics are
        # only used if start_round == 0, which continue mode excludes, but the
        # judger may cite baseline numbers so keep them available.
        baseline_eval_block, baseline_metrics = _eval_baseline(
            workspace, cfg, workspace.baseline_sha())
        if last_accepted is None:
            prior_metrics = baseline_metrics
            prior_eval_block = baseline_eval_block or ""
    else:
        baseline_eval_block, baseline_metrics = _eval_baseline(workspace, cfg, parent_sha)
        # round 0's "prior round" is the baseline. Updated to each round's eval after
        # that round is recorded. Both the raw text (for the record) and the parsed
        # metrics (for the FACTS block) are threaded forward.
        prior_eval_block = baseline_eval_block
        prior_metrics = baseline_metrics

    for round_id in range(start_round, n_rounds):
        print(f"\n[{stamp()}] === round {round_id + 1}/{n_rounds} ===", flush=True)

        # 1. proposer — reads the per-run repo (the SAME clone the executor
        #    commits into, so the round shas in history are valid objects here and
        #    the proposer can `git show`/`git diff` any prior round's actual
        #    changes). It never edits — the executor edits in a per-round worktree;
        #    the proposer's read of the repo is read-only (no working tree is
        #    checked out for it: the per-run repo is a bare-ish --no-checkout clone,
        #    so the proposer can only inspect commit history/diffs, not a live tree).
        #    It must NOT read other runs' repos (cross-run answer-copying); the
        #    per-run clone is physically isolated per run_dir.
        #    In static-proposal mode this step is skipped: the proposal is taken
        #    verbatim from the supplied batch (history is still recorded, but its
        #    feedback no longer picks the next direction).
        if static_proposals is not None:
            proposal = static_proposals[round_id]
            reflection, decision = "", "static"  # static mode: no proposer reflection
            print(f"[{stamp()}] proposal (static): {proposal[:150]}", flush=True)
        else:
            try:
                proposal_obj = proposer_mod.propose(
                    proposer_agent, goal=cfg["goal"], editable=cfg["editable_paths"],
                    frozen=cfg["frozen_paths"], history=store.history(),
                    base_sha=parent_sha, cwd=workspace.repo,
                    candidates_per_round=cfg.get("candidates_per_round", 1),
                    gate_block=gate_lines,
                )
            except (AgentError, ValueError) as exc:
                # A proposer contract failure cannot produce a candidate generation.
                print(f"[{stamp()}] proposer failed; aborting run: {exc}", flush=True)
                raise
            proposals_batch = proposal_obj.proposals
            reflection = proposal_obj.reflection
            print(f"[{stamp()}] proposals: {len(proposals_batch)} candidate(s)",
                  flush=True)

        if static_proposals is None:
            candidates = _run_candidates(
                proposals_batch, round_id, parent_sha, cfg, workspace,
                executor_agent, judger_agent, prior_metrics, baseline_metrics,
                metrics_schema, gate_lines,
            )
            winner = _select_winner(
                candidates,
                metrics_schema,
                prior_metrics=prior_metrics,
            )
            selected_candidate = winner.get("candidate") if winner else None
            selected_sha = winner.get("sha") if winner else None
            for candidate in candidates:
                candidate["selected"] = candidate.get("candidate") == selected_candidate
            next_base_sha = selected_sha or parent_sha
            if winner:
                print(f"[{stamp()}] selected candidate r{round_id}-c{selected_candidate}: "
                      f"{selected_sha[:10]}", flush=True)
                prior_metrics = winner.get("metrics") or prior_metrics
                prior_eval_block = winner.get("eval_block") or prior_eval_block
            else:
                print(f"[{stamp()}] no eligible candidate improved the incumbent; "
                      f"accepted base stays {parent_sha[:10]}", flush=True)
            store.append_generation(
                round_id, parent_sha=parent_sha,
                selected_candidate=selected_candidate, selected_sha=selected_sha,
                candidates=candidates, reflection=reflection,
            )
            _refresh_progress_plot(store)
            parent_sha = next_base_sha
            continue

        # 2. executor (+ gate + harness commit)
        worktree = workspace.add_worktree(round_id, parent_sha)
        try:
            result = executor_mod.execute(
                executor_agent, proposal=proposal, goal=cfg["goal"],
                editable=cfg["editable_paths"], frozen=cfg["frozen_paths"],
                workspace=workspace, worktree=worktree, round_id=round_id,
                gate_block=gate_lines,
            )
        except AgentError as exc:
            print(f"[{stamp()}] executor failed: {exc}", flush=True)
            workspace.remove_worktree(round_id)
            _record_failure(store, round_id, proposal, "executor failed: " + str(exc)[:200],
                            reflection=reflection, decision=decision, base_sha=parent_sha)
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
        #    run_eval returns (raw_text, metrics) — the harness parses the declared
        #    key=value lines so the judger never has to read numbers out of prose
        #    (which hallucinated a baseline number across 12 rounds before).
        eval_block = ""
        eval_metrics: dict = {}
        if result.sha and cfg["eval_commands"]:
            try:
                eval_block, eval_metrics = judger_mod.run_eval(
                    cfg["eval_commands"], cwd=worktree, metrics_schema=metrics_schema)
            except Exception as exc:  # timeout or subprocess error
                eval_block = f"(eval failed to run: {exc})"
                print(f"[{stamp()}] eval error: {exc}", flush=True)

        accepted = _candidate_accepted(result.sha, eval_metrics, metrics_schema)
        next_base_sha = result.sha if accepted else parent_sha
        if result.sha and not accepted:
            print(f"[{stamp()}] candidate rejected by hard gates; accepted base stays "
                  f"{parent_sha[:10]}", flush=True)

        try:
            judgment = judger_mod.judge(
                judger_agent, goal=cfg["goal"], proposal=proposal, sha=result.sha,
                reason=result.reason, parent_sha=parent_sha, workspace=workspace,
                eval_block=eval_block, cwd=worktree,
                metrics=eval_metrics,
                prior_metrics=prior_metrics,
                baseline_metrics=baseline_metrics,
                metrics_schema=metrics_schema,
            )
        except (AgentError, ValueError) as exc:
            # AgentError = claude call failed; ValueError = judger returned a
            # malformed score/feedback/risk. Hard-gate acceptance was already
            # computed by the harness, so a bad judgment does not override it.
            print(f"[{stamp()}] judger failed: {exc}", flush=True)
            _record_failure(store, round_id, proposal, "judger failed: " + str(exc)[:200], result.sha,
                            eval_block, eval_metrics, changed_paths=result.changed_paths,
                            reflection=reflection, decision=decision,
                            accepted=accepted, base_sha=next_base_sha)
            if accepted:
                prior_eval_block = eval_block or prior_eval_block
                prior_metrics = eval_metrics or prior_metrics
            parent_sha = next_base_sha
            continue
        finally:
            # the worktree must not leak, whatever the judger raised.
            workspace.remove_worktree(round_id)

        print(f"[{stamp()}] score={judgment.score:.2f}  risk={judgment.risk}  "
              f"feedback: {judgment.feedback[:120]}", flush=True)
        # The objective line: print the authoritative measured value (harness-parsed,
        # not the judger's prose) with vs-prior and vs-baseline deltas so the run
        # log shows each round's optimization result at a glance. Before, the speed
        # number lived inside feedback (truncated to 120 chars) or only in
        # history.jsonl, so the log reader couldn't see whether a round actually
        # improved. Best-effort: silent if no metrics schema / no objective value.
        _print_objective(eval_metrics, prior_metrics, baseline_metrics, metrics_schema)

        # 4. record + advance chain. eval_block + eval_metrics are stored so the
        #    NEXT round's judger gets this round's result as its "prior round"
        #    axis (raw text for the record + parsed metrics for the FACTS block).
        #    risk is stored for the harness's best selection (gate-pass + risk≠high).
        #    reflection + decision are stored as human-audit evidence (what the
        #    proposer reflected before choosing this direction) — NOT fed back
        #    into the next round's prompt (views.for_proposer doesn't project them).
        store.append(round_id, proposal, result.sha, judgment.score, judgment.feedback,
                     eval_block, eval_metrics=eval_metrics, risk=judgment.risk,
                     changed_paths=result.changed_paths,
                     reflection=reflection, decision=decision,
                     accepted=accepted, base_sha=next_base_sha)
        _refresh_progress_plot(store)
        if accepted:
            prior_eval_block = eval_block or prior_eval_block
            prior_metrics = eval_metrics or prior_metrics
        parent_sha = next_base_sha

    report = store.write_final_report(cfg["goal"])
    print(f"\n[{stamp()}] done. best={store.best_sha} (score {store.best_score:.2f})", flush=True)
    print(f"[{stamp()}] report: {report}", flush=True)
    print(f"[{stamp()}] working repo (for tracing): {workspace.repo}", flush=True)
    return _summary(store, workspace, run_dir_path)


def _summary(store: Store, workspace: Workspace, run_dir_path: Path) -> dict:
    """Build the run summary dict (shared by the normal exit and continue no-op)."""
    return {
        "best_sha": store.best_sha,
        "best_score": store.best_score,
        "rounds": len(store.history()),
        "run_dir": str(run_dir_path),
        "repo": str(workspace.repo),
    }


def _refresh_progress_plot(store: Store) -> None:
    try:
        history = store.history()
    except Exception as exc:
        print(f"[plot] warning: could not read {store.path}: {exc}", flush=True)
        return
    plot_mod.write_progress_png(store.run_dir, history, store.metrics_schema)


def _record_failure(store: Store, round_id: int, proposal: str, reason: str,
                    sha: str | None = None, eval_block: str = "",
                    eval_metrics: dict | None = None,
                    changed_paths: list[str] | None = None,
                    reflection: str = "", decision: str = "",
                    accepted: bool = False, base_sha: str | None = None) -> None:
    """Record a round where a role crashed, so history stays complete.

    A failed round never has a valid risk band; record risk as 'high' so it is
    never selected as best (a crashed judger/executor round is definitionally
    not a ship candidate). reflection/decision default empty — a proposer
    crash (L192) has neither; an executor/judger crash (L209/L251) passes the
    proposer's reflection/decision through so the audit trail is complete."""
    store.append(round_id, proposal, sha, 0.0, f"[loop failure] {reason}", eval_block,
                 eval_metrics=eval_metrics or {}, risk="high",
                 changed_paths=changed_paths or [],
                 reflection=reflection, decision=decision,
                 accepted=accepted, base_sha=base_sha)
    _refresh_progress_plot(store)


def _run_candidates(proposals: list[proposer_mod.Proposal], round_id: int,
                    parent_sha: str, cfg: dict, workspace: Workspace,
                    executor_agent: Agent, judger_agent: Agent,
                    prior_metrics: dict, baseline_metrics: dict,
                    metrics_schema: dict | None, gate_lines: str) -> list[dict]:
    """Run one generation's candidates, possibly concurrently."""
    max_workers = min(cfg.get("max_workers", 1), max(1, len(proposals)))
    if max_workers <= 1 or len(proposals) <= 1:
        return [
            _run_one_candidate(i, proposal, round_id, parent_sha, cfg, workspace,
                               executor_agent, judger_agent, prior_metrics,
                               baseline_metrics, metrics_schema, gate_lines)
            for i, proposal in enumerate(proposals)
        ]
    results: list[dict | None] = [None] * len(proposals)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_one_candidate, i, proposal, round_id, parent_sha, cfg,
                        workspace, executor_agent, judger_agent, prior_metrics,
                        baseline_metrics, metrics_schema, gate_lines): i
            for i, proposal in enumerate(proposals)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as exc:
                # Last-resort guard: candidate failures should stay local and not
                # kill the whole generation.
                p = proposals[i]
                results[i] = _candidate_failure(
                    i, p, f"candidate worker failed: {exc}", parent_sha)
    return [r for r in results if r is not None]


def _run_one_candidate(candidate_id: int, proposal: proposer_mod.Proposal,
                       round_id: int, parent_sha: str, cfg: dict,
                       workspace: Workspace, executor_agent: Agent,
                       judger_agent: Agent, prior_metrics: dict,
                       baseline_metrics: dict,
                       metrics_schema: dict | None, gate_lines: str) -> dict:
    """Executor + eval + judger for one candidate."""
    worktree_id = f"{round_id}-c{candidate_id}"
    worktree = None
    result = None
    eval_block = ""
    eval_metrics: dict = {}
    accepted = False
    try:
        print(f"[{stamp()}] candidate r{round_id}-c{candidate_id} "
              f"(family={proposal.family}, decision={proposal.decision}): "
              f"{proposal.proposal[:120]}", flush=True)
        worktree = workspace.add_worktree(worktree_id, parent_sha)
        result = executor_mod.execute(
            executor_agent, proposal=proposal.proposal, goal=cfg["goal"],
            editable=cfg["editable_paths"], frozen=cfg["frozen_paths"],
            workspace=workspace, worktree=worktree, round_id=worktree_id,
            gate_block=gate_lines,
        )
        if result.sha:
            print(f"[{stamp()}] candidate r{round_id}-c{candidate_id} committed: "
                  f"{result.sha} ({len(result.changed_paths)} files)", flush=True)
        else:
            print(f"[{stamp()}] candidate r{round_id}-c{candidate_id} no commit: "
                  f"{result.reason}", flush=True)
        if result.sha and cfg["eval_commands"]:
            try:
                eval_block, eval_metrics = judger_mod.run_eval(
                    cfg["eval_commands"], cwd=worktree,
                    metrics_schema=metrics_schema)
            except Exception as exc:
                eval_block = f"(eval failed to run: {exc})"
                print(f"[{stamp()}] candidate r{round_id}-c{candidate_id} "
                      f"eval error: {exc}", flush=True)
        accepted = _candidate_accepted(result.sha, eval_metrics, metrics_schema)
        judgment = judger_mod.judge(
            judger_agent, goal=cfg["goal"], proposal=proposal.proposal,
            sha=result.sha, reason=result.reason, parent_sha=parent_sha,
            workspace=workspace, eval_block=eval_block, cwd=worktree,
            metrics=eval_metrics, prior_metrics=prior_metrics,
            baseline_metrics=baseline_metrics, metrics_schema=metrics_schema,
        )
        print(f"[{stamp()}] candidate r{round_id}-c{candidate_id} "
              f"score={judgment.score:.2f} risk={judgment.risk} "
              f"feedback: {judgment.feedback[:120]}", flush=True)
        _print_objective(eval_metrics, prior_metrics, baseline_metrics, metrics_schema)
        return {
            "candidate": candidate_id,
            "family": proposal.family,
            "decision": proposal.decision,
            "proposal": proposal.proposal,
            "sha": result.sha,
            "score": judgment.score,
            "risk": judgment.risk,
            "feedback": judgment.feedback,
            "eval_block": eval_block,
            "metrics": eval_metrics,
            "changed_paths": result.changed_paths,
            "accepted": accepted,
            "selected": False,
        }
    except (AgentError, ValueError) as exc:
        changed_paths = result.changed_paths if result else []
        sha = result.sha if result else None
        return _candidate_failure(candidate_id, proposal, str(exc), parent_sha,
                                  sha=sha, eval_block=eval_block,
                                  eval_metrics=eval_metrics,
                                  changed_paths=changed_paths,
                                  accepted=accepted)
    finally:
        if worktree is not None:
            workspace.remove_worktree(worktree_id)


def _candidate_failure(candidate_id: int, proposal: proposer_mod.Proposal,
                       reason: str, parent_sha: str, sha: str | None = None,
                       eval_block: str = "", eval_metrics: dict | None = None,
                       changed_paths: list[str] | None = None,
                       accepted: bool = False) -> dict:
    return {
        "candidate": candidate_id,
        "family": proposal.family,
        "decision": proposal.decision,
        "proposal": proposal.proposal,
        "sha": sha,
        "score": 0.0,
        "risk": "high",
        "feedback": f"[loop failure] {reason[:200]}",
        "eval_block": eval_block,
        "metrics": eval_metrics or {},
        "changed_paths": changed_paths or [],
        "accepted": accepted,
        "selected": False,
        "base_sha": parent_sha,
    }


def _select_winner(candidates: list[dict],
                   metrics_schema: dict | None,
                   prior_metrics: dict | None = None) -> dict | None:
    """Select an eligible candidate only when it improves the incumbent."""
    eligible = []
    if metrics_schema:
        obj = metrics_schema["objective"]
        key = obj["key"]
        gate_keys = [g["key"] for g in metrics_schema.get("gates", [])]
        for c in candidates:
            metrics = c.get("metrics") or {}
            if not c.get("sha"):
                continue
            if str(c.get("risk", "high")).lower() == "high":
                continue
            if not all(metrics.get(gk) is True for gk in gate_keys):
                continue
            if not isinstance(metrics.get(key), (int, float)):
                continue
            eligible.append(c)
        if not eligible:
            return None
        lower = obj["lower_is_better"]
        direction = 1 if lower else -1
        winner = min(eligible, key=lambda c: (
            direction * c["metrics"][key],
            -(c.get("score") or 0.0),
            c.get("candidate") or 0,
        ))
        prior_value = (prior_metrics or {}).get(key)
        if isinstance(prior_value, (int, float)):
            winner_value = winner["metrics"][key]
            improved = winner_value < prior_value if lower else winner_value > prior_value
            if not improved:
                return None
        return winner
    for c in candidates:
        if c.get("sha") and str(c.get("risk", "high")).lower() != "high":
            eligible.append(c)
    if not eligible:
        return None
    return max(eligible, key=lambda c: (c.get("score") or 0.0,
                                       -(c.get("candidate") or 0)))


def _candidate_accepted(candidate_sha: str | None, metrics: dict | None,
                        metrics_schema: dict | None) -> bool:
    """Whether a candidate becomes the next cumulative base.

    Every declared gate must be explicitly True. Missing/unknown gate values are
    rejection, while configs without gates keep the legacy commit-on-SHA behavior.
    """
    if not candidate_sha:
        return False
    gates = (metrics_schema or {}).get("gates", [])
    if not gates:
        return True
    values = metrics or {}
    return all(values.get(g["key"]) is True for g in gates)


def _resume_chain(history: list[dict], baseline_sha: str,
                  metrics_schema: dict | None) -> tuple[str, dict | None]:
    """Return the last accepted SHA and its record, skipping rejected tails.

    New records carry an explicit accepted flag. For old history, infer the flag
    from the currently configured hard gates so a legacy correctness-failing tail
    is not accidentally resumed. Without gates, legacy SHA behavior is preserved.
    """
    for record in reversed(history):
        if "candidates" in record:
            selected_sha = record.get("selected_sha")
            if selected_sha:
                selected = next((c for c in record.get("candidates", [])
                                 if c.get("selected")), None)
                return selected_sha, selected or record
            continue
        sha = record.get("sha")
        if "accepted" in record:
            accepted = bool(sha) and record.get("accepted") is True
        else:
            accepted = _candidate_accepted(
                sha, record.get("metrics") or {}, metrics_schema)
        if accepted:
            return sha, record
    return baseline_sha, None


def _eval_baseline(workspace: Workspace, cfg: dict, baseline_sha: str) -> tuple[str | None, dict]:
    """Run the eval commands once on the unoptimized baseline commit.

    Returns (eval_block, metrics) — the raw text (kept for the record) and the
    parsed metrics dict (the authoritative baseline numbers the judger's FACTS
    block cites). (None, {}) if no eval is configured or the baseline eval
    failed. Best-effort: failures are logged and swallowed — the run continues
    with prior-round-only comparison. Uses a throwaway worktree on the baseline
    SHA (the bare per-run clone has no working tree, same reason the per-round
    eval uses a worktree).
    """
    if not cfg["eval_commands"]:
        return None, {}
    print(f"[{stamp()}] running baseline eval (on {baseline_sha[:10]}) for the judger's "
          f"vs-baseline axis...", flush=True)
    wt = None
    try:
        wt = workspace.add_worktree("baseline", baseline_sha)
        block, metrics = judger_mod.run_eval(
            cfg["eval_commands"], cwd=wt, metrics_schema=cfg.get("metrics"))
        print(f"[{stamp()}] baseline eval done.", flush=True)
        return block, metrics
    except Exception as exc:  # worktree add or eval failure
        print(f"[{stamp()}] baseline eval failed (judger will use prior-round-only "
              f"comparison): {exc}", flush=True)
        return None, {}
    finally:
        if wt is not None:
            workspace.remove_worktree("baseline")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _print_objective(metrics: dict | None, prior: dict | None, baseline: dict | None,
                     schema: dict | None) -> None:
    """Print the round's measured objective value with vs-prior and vs-baseline deltas.

    All three values are harness-parsed (authoritative) — never the judger's prose,
    which is the number-hallucination vector this loop was built to avoid. Silent
    (no line) when the eval has no metrics schema or this round produced no
    objective value (e.g. no-commit / failed-eval rounds): the score+feedback line
    above already says "no commit" for those, so a redundant "objective: unknown"
    line would just be noise. lower_is_better is read from the schema so the delta
    arrow points the right way regardless of whether the objective is ms/evt,
    binary size, or throughput.
    """
    if not schema or not metrics:
        return
    obj = schema.get("objective", {})
    key = obj.get("key")
    if not key:
        return
    val = metrics.get(key)
    if val is None:
        return  # no measurement this round (no commit / eval crashed) — stay silent
    lower_is_better = obj.get("lower_is_better", True)
    def _fmt_delta(this, other, label):
        if not isinstance(this, (int, float)) or not isinstance(other, (int, float)) or other == 0:
            return None
        pct = (this - other) / other * 100.0
        improved = (pct < 0) if lower_is_better else (pct > 0)
        arrow = "↓ better" if improved else ("↑ worse" if pct != 0 else "= same")
        return f"{label} {other:g} ({pct:+.1f}%, {arrow})"
    parts = [f"{key}={val:g}"]
    for other, label in ((prior, "vs prior"), (baseline, "vs baseline")):
        if other:
            d = _fmt_delta(val, other.get(key), label)
            if d:
                parts.append(d)
    print(f"[{stamp()}] objective: " + "  |  ".join(parts), flush=True)


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
