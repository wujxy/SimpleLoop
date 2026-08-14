"""Outer scheduler for Researcher -> Executor -> Harness experiments."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import yaml

from .roles.agent import Agent
from . import candidate_worker
from .candidate import CandidateResult
from . import config as config_mod
from .execution import build_backend
from .execution.base import InfraRoundError, RoundJournal
from .harness import evals
from .reporting import plot as plot_mod
from .stages.proposer import Proposal, ProposalBatch, ProposerRequest
from .harness import views
from .harness.handoff import write_handoff
from .harness.store import Store, best_candidate as _best_candidate
from .round import RoundResult
from .stages.selector import select_candidate
from .reporting.telemetry import RunTelemetry
from .container.runtime import ApptainerRuntime, MountMap, world_mount_map
from .harness.workspace import Workspace
from .processes import run_signal_handlers
from .self_repo import SelfRepo


class BaselineAcceptanceError(RuntimeError):
    """Raised when the configured runtime cannot pass the task baseline."""


class RunLockError(RuntimeError):
    """Raised when another simpleloop process already holds the run_dir."""


INFLIGHT_NAME = "inflight_round.json"


class _InflightJournal(RoundJournal):
    """The loop-owned in-flight round file: ONE atomic unit carrying
    everything a --continue needs — the round meta (round_id, parent_sha,
    proposals with their finding targets) plus the backend's opaque jobs
    table. The backend calls save(jobs) on every job-state transition but
    never sees the meta; clear() happens only when the round produced
    business-terminal candidates. The file appears at the first save (after
    job submission), so a crash before that simply re-proposes the round —
    there is no half-written meta to reconcile."""

    def __init__(self, path: Path, meta: dict):
        self.path = path
        self.meta = meta

    def save(self, jobs: list[dict]) -> None:
        payload = {**self.meta, "jobs": jobs}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                       + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass
class RunContext:
    """Per-run fixtures shared by every round: config, runtime, agents, stores.
    Mutable per-round search state (parent_sha, prior_metrics) stays in run()."""
    cfg: dict
    run_dir: Path | None = None
    runtime: ApptainerRuntime | None = None
    workspace: Workspace | None = None
    store: Store | None = None
    telemetry: RunTelemetry | None = None
    executor_agent: Agent | None = None
    self_executor_agent: Agent | None = None
    prompt_dir: Path | None = None
    gate_lines: str = ""
    baseline_metrics: dict = field(default_factory=dict)
    execution_backend: object | None = None
    self_repo: SelfRepo | None = None

    @property
    def metrics_schema(self) -> dict | None:
        return self.cfg.get("metrics")

def _acquire_run_lock(run_dir: Path) -> int | None:
    """flock run_dir/.lock exclusively so two runs cannot interleave writes to
    history.jsonl. flock (not an O_EXCL pid file) because the kernel releases
    it on any process death — no stale lock to clean up after a SIGKILL.

    Returns the held fd, or None when the filesystem does not support flock
    (some NFS/Lustre mounts): there we warn and run unprotected rather than
    block a legitimate long run.
    """
    lock_path = run_dir / ".lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = ""
        try:
            holder = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
        except OSError:
            pass
        os.close(fd)
        raise RunLockError(
            f"run_dir {run_dir} is locked by another simpleloop run"
            + (f" ({holder})" if holder else "")
            + "; wait for it to finish or use a different --run-dir")
    except OSError as exc:
        os.close(fd)
        print(f"[{stamp()}] warning: could not flock {lock_path} ({exc}); "
              "concurrent-run protection is DISABLED on this filesystem", flush=True)
        return None
    # Holder info is diagnostics only — the lock semantics live in flock.
    info = json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                       "started_at": stamp()})
    os.ftruncate(fd, 0)
    os.pwrite(fd, info.encode("utf-8"), 0)
    return fd


def _release_run_lock(fd: int | None) -> None:
    """Unlock and close; the .lock file stays (deleting would race a waiter)."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_config_snapshot(cfg: dict, config_path: str | Path,
                           run_dir: Path) -> None:
    """Persist run provenance: the RESOLVED config (overwritten every run, so a
    --continue with a bumped max_rounds is reflected) plus a verbatim copy of
    the original file (written once). `simpleloop plot`/`export` read the
    resolved snapshot back instead of requiring --config."""
    snapshot = run_dir / config_mod.RESOLVED_SNAPSHOT_NAME
    # default=str: config.load only emits str/int/bool/list, but callers may
    # inject Path values programmatically — degrade them to their string form.
    snapshot.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    src = Path(config_path).expanduser().resolve()
    orig = run_dir / f"config.orig{src.suffix}"
    if src.is_file() and not orig.exists():
        shutil.copyfile(src, orig)


def run(config_path: str | Path, run_dir: str | Path,
        proposals: str | Path | list[str] | None = None,
        continue_run: bool = False,
        target_rounds: int | None = None,
        prompt_dir: str | Path | None = None) -> dict:
    """Run the full loop. Returns a summary dict.

    `proposals` switches to static-proposal mode; `continue_run` resumes an
    existing run-dir with loop.max_rounds as the target TOTAL round count."""
    cfg = config_mod.load(config_path)
    # Resolve run_dir now: git worktrees and Popen cwd must agree on location.
    run_dir_path = Path(run_dir).resolve()
    run_dir_path.mkdir(parents=True, exist_ok=True)

    # Prevent accidental overwriting of existing runs: if the run directory
    # already has history.jsonl with content, require --continue or explicit cleanup.
    if not continue_run:
        history_path = run_dir_path / "history.jsonl"
        if history_path.exists() and history_path.stat().st_size > 0:
            raise ValueError(
                f"run-dir {run_dir} already contains existing runs (history.jsonl found). "
                f"Use --continue to resume, or remove the directory to start fresh."
            )

    lock_fd = _acquire_run_lock(run_dir_path)
    try:
        _write_config_snapshot(cfg, config_path, run_dir_path)
        with run_signal_handlers():
            return _run_locked(
                cfg, run_dir_path, proposals, continue_run, target_rounds,
                Path(prompt_dir).resolve() if prompt_dir is not None else None,
            )
    finally:
        _release_run_lock(lock_fd)


def _run_locked(cfg: dict, run_dir_path: Path,
                proposals: str | Path | list[str] | None,
                continue_run: bool,
                target_rounds: int | None = None,
                prompt_dir: Path | None = None) -> dict:
    static_proposals = _load_proposals(proposals)
    if static_proposals is not None and continue_run:
        raise ValueError("--continue cannot be combined with --proposals: continue "
                         "resumes a researcher run from its history, but "
                         "--proposals drives rounds from a fixed batch.")
    if static_proposals is not None:
        if target_rounds is not None:
            raise ValueError(
                "target_rounds cannot be combined with static proposals"
            )
        n_rounds = len(static_proposals)
        print(f"[{stamp()}] STATIC-PROPOSAL mode: {n_rounds} round(s) from the "
              f"supplied batch (config max_rounds={cfg['max_rounds']} ignored)", flush=True)
    else:
        if cfg.get("roles", {}).get("researcher") is None:
            raise config_mod.ConfigError(
                "roles.researcher: required for agent-driven runs"
            )
        if target_rounds is None:
            n_rounds = cfg["max_rounds"]
        elif (
            not isinstance(target_rounds, int)
            or isinstance(target_rounds, bool)
            or not 1 <= target_rounds <= cfg["max_rounds"]
        ):
            raise ValueError(
                "target_rounds must be between 1 and configured max_rounds"
            )
        else:
            n_rounds = target_rounds

    ctx = _build_context(
        cfg,
        run_dir_path,
        resume=continue_run,
        prompt_dir=prompt_dir,
    )

    print(f"[{stamp()}] setting up working repo (clone --local from {cfg['repo_path']})", flush=True)
    ctx.workspace.setup()

    # S3a: snapshot the proposer source into run_dir/self/repo (S0) on a fresh run,
    # or resume the existing self life-history on --continue. The worker subprocess
    # resolves `import proposer` to this snapshot (see proposer_lane_worker).
    ctx.self_repo = SelfRepo(run_dir_path)
    ctx.self_repo.setup(resume=continue_run)
    # S3c.2: RSI self-review is opt-in via cfg["rsi"]["first_self_review_round"].
    # Seed the first self-review only on a fresh run (resume keeps the persisted
    # commitment). If unset, self-review never triggers (task-only run).
    if not continue_run:
        _first = (cfg.get("rsi") or {}).get("first_self_review_round")
        if (isinstance(_first, int) and not isinstance(_first, bool)
                and _first >= 0
                and ctx.self_repo.next_self_review_round is None):
            ctx.self_repo.update_commitment(next_self_review_round=_first)
            print(f"[{stamp()}] RSI self-review enabled: first review at round "
                  f"{_first + 1}", flush=True)
    preflight_id = "executor-preflight"
    preflight_worktree = ctx.workspace.add_worktree(
        preflight_id, ctx.workspace.baseline_sha(),
    )
    try:
        ctx.runtime.executor_preflight(
            worktree=preflight_worktree,
            mounts=world_mount_map(cfg),
        )
    finally:
        ctx.workspace.remove_worktree(preflight_id)
    print("executor preflight: PASS", flush=True)
    print(f"[{stamp()}] baseline sha: {ctx.workspace.baseline_sha()}", flush=True)

    start = _starting_state(ctx, continue_run, n_rounds)
    if start is None:  # --continue with nothing left to do
        return _summary(ctx, run_dir_path)
    start_round, parent_sha, prior_metrics = start
    display_rounds = cfg["max_rounds"] if target_rounds is not None else n_rounds
    rsi_tally = {"KEEP": 0, "CHANGE": 0}  # anti-self-justification signal (S3c.2)

    for round_id in range(start_round, n_rounds):
        print(
            f"\n[{stamp()}] === current round "
            f"{round_id + 1}/{display_rounds} ===",
            flush=True,
        )
        if (
            round_id == start_round
            and target_rounds is not None
            and target_rounds < cfg["max_rounds"]
        ):
            print(
                f"[{stamp()}] Next optimizer will be started after round "
                f"{target_rounds}",
                flush=True,
            )

        # If the frontend crashed during a previous proposer stage, kill any
        # orphan proposer-lane jobs and clear the marker before re-proposing.
        ctx.execution_backend.cleanup_proposer_orphans()

        # S3c.2: RSI self-review mode-switch. If the Scientist's own commitment
        # says to re-examine itself by this round, run a self-review round
        # instead of a task round, record it, advance the commitment, and skip
        # candidates/selection/history (a self-review does not touch the task
        # incumbent or the task ledger).
        _nsrr = ctx.self_repo.next_self_review_round
        if _nsrr is not None and round_id >= _nsrr:
            _decision = _run_self_review_round(ctx, round_id)
            rsi_tally[_decision] = rsi_tally.get(_decision, 0) + 1
            print(f"[{stamp()}] [rsi] self-review r{round_id + 1}: {_decision}  "
                  f"(running KEEP/CHANGE = {rsi_tally['KEEP']}/{rsi_tally['CHANGE']})",
                  flush=True)
            continue

        inflight = _load_inflight(ctx.run_dir)
        if inflight is not None:
            if inflight.get("round_id") != round_id:
                raise ValueError(
                    f"--continue: inflight_round.json is for round "
                    f"{inflight.get('round_id')} but the loop is at round "
                    f"{round_id}; delete {ctx.run_dir / INFLIGHT_NAME} to "
                    "re-propose this round, or fix loop.max_rounds.")
            print(f"[{stamp()}] resuming in-flight round {round_id + 1} "
                  "from inflight_round.json (proposer skipped)", flush=True)
            # The journal's meta was written by the original session; keep it
            # whole so subsequent saves preserve the proposer's outputs.
            journal = _InflightJournal(
                ctx.run_dir / INFLIGHT_NAME,
                meta={k: v for k, v in inflight.items() if k != "jobs"})
            proposals_meta = inflight.get("proposals") or []
            proposal_batch = ProposalBatch(tuple(
                Proposal(
                    instruction=(
                        str(item["instruction"])
                        if isinstance(item, dict) else str(item)
                    ),
                    evidence_refs=tuple(
                        str(ref)
                        for ref in (
                            item.get("evidence_refs") or ()
                            if isinstance(item, dict) else ()
                        )
                    ),
                )
                for item in proposals_meta
            ))
            proposal_instructions = [
                proposal.instruction for proposal in proposal_batch.proposals
            ]
            try:
                candidates = ctx.execution_backend.resume_round(
                    inflight.get("jobs") or [], round_id=round_id,
                    parent_sha=inflight["parent_sha"], journal=journal)
            except InfraRoundError as exc:
                print(f"[{stamp()}] {exc}", flush=True)
                print(f"[{stamp()}] round {round_id + 1} still not complete; "
                      "fix the infrastructure issue and re-run with "
                      "--continue again.", flush=True)
                return _summary(ctx, run_dir_path)
        else:
            proposal_batch = _next_proposals(
                ctx, static_proposals, round_id, parent_sha)
            _write_proposer_trace(ctx, round_id, proposal_batch)
            _write_proposals_handoff(ctx, round_id, parent_sha,
                                     proposal_batch)
            if proposal_batch.abstained:
                # Zero-candidate round: the Scientist judged no experiment
                # worth its execution cost. Skip the executor entirely and
                # record the abstention so --continue counts the round as
                # consumed and the next round can see why nothing ran.
                print(f"[{stamp()}] proposer abstained round {round_id + 1}: "
                      f"{proposal_batch.abstention.reason}", flush=True)
                candidates = ()
                journal = None
            else:
                proposal_instructions = [
                    proposal.instruction
                    for proposal in proposal_batch.proposals
                ]
                proposals_meta = [
                    {
                        "instruction": prop.instruction,
                        "evidence_refs": list(prop.evidence_refs),
                    }
                    for prop in proposal_batch.proposals
                ]
                journal = _InflightJournal(
                    ctx.run_dir / INFLIGHT_NAME,
                    meta={
                        "round_id": round_id,
                        "parent_sha": parent_sha,
                        "proposals": proposals_meta,
                    })
                try:
                    candidates = ctx.execution_backend.run_candidates(
                        proposals=proposal_instructions, round_id=round_id,
                        parent_sha=parent_sha, journal=journal)
                except InfraRoundError as exc:
                    # The round is not consumed: inflight_round.json stays on
                    # disk so --continue can resume; exit for human recovery.
                    print(f"[{stamp()}] {exc}", flush=True)
                    print(f"[{stamp()}] round {round_id + 1} not recorded; "
                          "fix the infrastructure issue and re-run with "
                          "--continue (or delete inflight_round.json to "
                          "re-propose).", flush=True)
                    return _summary(ctx, run_dir_path)
        candidates = _finalize_candidates(ctx, tuple(candidates))
        objective = ctx.metrics_schema["objective"]
        prior_value = (prior_metrics or {}).get(objective["key"])
        selection = select_candidate(
            candidates=candidates,
            objective_key=objective["key"],
            lower_is_better=objective["lower_is_better"],
            incumbent_value=(
                float(prior_value)
                if isinstance(prior_value, (int, float))
                and not isinstance(prior_value, bool) else None
            ),
            require_improvement=static_proposals is None,
        )
        winner = next(
            (
                candidate for candidate in candidates
                if candidate.candidate_id == selection.candidate_id
            ),
            None,
        )
        if proposal_batch.abstention is None:
            _print_round_performance(
                round_id, candidates, ctx.metrics_schema, prior_metrics,
            )
        selected_candidate = selection.candidate_id
        selected_sha = selection.sha
        if winner:
            print(f"[{stamp()}] selected candidate r{round_id}-c{selected_candidate}: "
                  f"{selected_sha[:10]}", flush=True)
            prior_metrics = dict(winner.metrics) or prior_metrics
        else:
            if proposal_batch.abstention is not None:
                reason = "proposer abstained (no experiment worth its cost)"
            else:
                reason = ("candidate rejected by hard gates"
                          if static_proposals is not None
                          else "no eligible candidate improved the incumbent")
            print(f"[{stamp()}] {reason}; parent stays {parent_sha[:10]}",
                  flush=True)
        round_result = RoundResult(
            round_id=round_id,
            parent_sha=parent_sha,
            proposals=proposal_batch,
            candidates=candidates,
            selection=selection,
            telemetry=ctx.telemetry.snapshot(persist=True),
        )
        ctx.store.append_round(round_result)
        if journal is not None:
            journal.clear()
        _refresh_progress_plot(ctx.store, ctx.telemetry.plot_context())
        parent_sha = round_result.next_sha

    summary = _summary(ctx, run_dir_path)
    print(f"\n[{stamp()}] done. best={summary['best_sha']}", flush=True)
    print(f"[{stamp()}] working repo (for tracing): {ctx.workspace.repo}", flush=True)
    return summary


def _assert_executor_ready(cfg: dict) -> None:
    """Fail fast before submitting any job. The executor `claude` runs on
    isolated worker nodes and needs both an explicit endpoint (from config)
    and a forwarded credential (from the environment). Without this guard a
    missing config silently produces round after round of EXECUTOR_FAILED
    (ConnectionRefused) — exactly the 12-round failure this guards against."""
    executor = (cfg.get("roles") or {}).get("executor")
    if not executor or not str(executor.get("base_url", "")).strip():
        raise config_mod.ConfigError(
            "roles.executor.base_url: required for candidate execution — "
            "declare roles.executor (api/model/base_url) in the config"
        )
    if not (os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")):
        raise config_mod.ConfigError(
            "ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) must be exported in "
            "the shell running 'simpleloop run'; it is forwarded to worker "
            "nodes via job_env.sh so the in-job claude can authenticate"
        )


def _build_context(
    cfg: dict,
    run_dir_path: Path,
    *,
    resume: bool,
    prompt_dir: Path | None = None,
) -> RunContext:
    """Construct the run's fixed fixtures: runtime, executor agent, workspace,
    store, and telemetry. The proposer runs as a subprocess
    (simpleloop.proposer_lane_worker), not as an in-context agent."""
    telemetry = RunTelemetry(run_dir_path, resume=resume)
    runtime = ApptainerRuntime(
        image=cfg["runtime_image"],
        binds=cfg["runtime_binds"],
        run_dir=run_dir_path,
    )
    for line in runtime.summary_lines():
        print(line, flush=True)
    runtime.preflight()
    print("preflight: PASS", flush=True)
    _assert_executor_ready(cfg)

    timeout = cfg.get("agent_timeout_seconds", 3600)
    max_output_tokens = cfg.get("agent_max_output_tokens", 64000)
    roles = cfg["roles"]
    executor = roles["executor"]
    executor_agent = Agent(runtime=runtime, command="claude",
                           timeout_seconds=timeout,
                           allowed_tools="Read,Edit,Write,Bash",
                           max_output_tokens=max_output_tokens,
                           model=executor["model"],
                           base_url=executor["base_url"],
                           usage_observer=telemetry.record_usage,
                           mounts=world_mount_map(cfg))
    workspace = Workspace(
        run_dir=run_dir_path,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    store = Store(run_dir_path, metrics_schema=cfg["metrics"],
                  history_eval_cap=cfg.get("eval_history_cap_chars", 6000))
    # Same gate bullet lines go into both the proposer's and executor's prompt.
    gate_lines = views.gate_block(cfg.get("metrics"))
    ctx = RunContext(
        cfg=cfg, run_dir=run_dir_path, runtime=runtime, workspace=workspace,
        store=store, telemetry=telemetry,
        executor_agent=executor_agent,
        prompt_dir=prompt_dir, gate_lines=gate_lines,
    )
    ctx.execution_backend = build_backend(ctx)
    return ctx


def _starting_state(ctx: RunContext, continue_run: bool,
                    n_rounds: int) -> tuple[int, str, dict] | None:
    """Run the baseline eval and resolve where the round loop starts.

    Returns (start_round, parent_sha, prior_metrics), or None when --continue
    finds nothing left to do. The baseline eval doubles as the runtime
    acceptance test: a failure aborts before any optimization role is called."""
    baseline_sha = ctx.workspace.baseline_sha()
    if not continue_run:
        _, baseline_metrics = ctx.execution_backend.eval_baseline(
            baseline_sha=baseline_sha)
        ctx.telemetry.set_baseline(baseline_metrics)
        ctx.baseline_metrics = baseline_metrics
        return 0, baseline_sha, baseline_metrics

    done = ctx.store.history()
    if not done:
        raise ValueError(
            f"--continue: run-dir {ctx.run_dir} has no history.jsonl rounds; "
            "drop --continue and start a fresh run."
        )
    # S3c.2: self-review rounds consume a round_id but write no history.jsonl
    # line, so len(history) underestimates the loop position after a self-review.
    # Resume at max(last task round, last self-review round) + 1. parent_sha /
    # prior_metrics still come from the last TASK round (self-reviews don't
    # change the task incumbent) via _resume_chain below.
    last_task_round = done[-1].get("round", len(done) - 1)
    last_self_round = ctx.self_repo.last_review_round()
    start_round = max(last_task_round, last_self_round if last_self_round is not None else -1) + 1
    if start_round >= n_rounds:
        print(f"[{stamp()}] --continue: {start_round} round(s) already recorded, "
              f"max_rounds={n_rounds} -- nothing to do. Bump loop.max_rounds in "
              f"the config to add more rounds.", flush=True)
        _refresh_progress_plot(ctx.store, ctx.telemetry.plot_context())
        return None
    parent_sha, last_selected = _resume_chain(done, baseline_sha)
    prior_metrics = (last_selected or {}).get("metrics") or {}
    print(f"[{stamp()}] --continue: resuming from round {start_round + 1} "
          f"(parent_sha={parent_sha[:10]}, {start_round} round(s) already done)",
          flush=True)
    # Re-evaluate the baseline so resumed telemetry stays comparable.
    _, baseline_metrics = ctx.execution_backend.eval_baseline(
        baseline_sha=baseline_sha)
    ctx.baseline_metrics = baseline_metrics
    if last_selected is None:
        prior_metrics = baseline_metrics
    return start_round, parent_sha, prior_metrics


def _write_proposer_trace(ctx: RunContext, round_id: int,
                          proposal_result: ProposalBatch) -> None:
    """Persist the round's non-authoritative proposer trajectory. This is
    behavioral telemetry for offline analysis only — it is NEVER injected into
    a future round's startup pack and carries no fact authority over the
    immutable Experiment Ledger."""
    trace = proposal_result.trace
    if not trace:
        return  # static-proposal mode produces no deliberation trace
    trace_dir = ctx.run_dir / "proposer_traces"
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / f"r{round_id}.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[{stamp()}] proposer trace write skipped: {exc}", flush=True)


def _write_proposals_handoff(
    ctx: RunContext, round_id: int, parent_sha: str,
    proposal_result: ProposalBatch,
) -> None:
    """Persist the proposer's output the moment it finishes — before any
    executor starts. Captures Host-visible proposal facts and the abstention
    reason so the round can be traced if the process dies mid-execution."""
    proposals = []
    for i, prop in enumerate(proposal_result.proposals):
        proposals.append({
            "index": i,
            "instruction": prop.instruction,
            "evidence_refs": list(prop.evidence_refs),
        })
    write_handoff(ctx.run_dir, round_id, "proposals.json", {
        "round_id": round_id,
        "parent_sha": parent_sha,
        "abstained": proposal_result.abstained,
        "abstain_reason": (
            proposal_result.abstention.reason
            if proposal_result.abstention else None
        ),
        "proposals": proposals,
        "trace": proposal_result.trace,
    })


def _next_proposals(ctx: RunContext, static_proposals: list[str] | None,
                    round_id: int, parent_sha: str,
                    ) -> ProposalBatch:
    """Return one round's structured research proposals.

    Normal mode creates one writable lane workspace per lane (each a fresh git
    worktree at ``parent_sha``) and runs the Proposer Orchestrator, which fans
    the lanes out — every lane researches in its own isolated workspace.
    Static mode wraps each supplied instruction in a Host proposal."""
    if static_proposals is not None:
        proposal_text = static_proposals[round_id]
        print(f"[{stamp()}] proposal (static): {proposal_text[:150]}", flush=True)
        return ProposalBatch((Proposal(instruction=proposal_text),))

    try:
        proposal_obj = ctx.execution_backend.run_proposer_lanes(
            ProposerRequest(
                round_id=round_id,
                goal=str(ctx.cfg["goal"]),
                incumbent_sha=parent_sha,
            )
        )
    except ValueError as exc:
        # A proposer contract failure cannot produce a candidate generation.
        print(f"[{stamp()}] proposer failed; aborting run: {exc}", flush=True)
        raise
    print(f"[{stamp()}] proposals: {len(proposal_obj.proposals)} candidate(s)",
          flush=True)
    return proposal_obj


# S3c.2: if a self-review payload carries no next_review_after_rounds (a CHANGE
# without a suggested commitment — S3d has not acted on it yet), defer
# re-examination by this many task rounds. KEEP requires a commitment and the
# proposer's abstain path defaults to 3, so this only hits a bare CHANGE.
_DEFAULT_SELF_REVIEW_DEFER = 8


def _get_self_executor(ctx: RunContext) -> Agent:
    """The self-executor: the same claude-p Agent as the task executor, but under
    a contained mount that exposes only the self-worktree (the whole worktree
    read-only, ``proposer/`` writable) so it can edit the proposer source without
    seeing or corrupting the rest of the run. Built lazily — only when a CHANGE
    actually happens (RSI S3d)."""
    if ctx.self_executor_agent is None:
        executor = ctx.cfg["roles"]["executor"]
        ctx.self_executor_agent = Agent(
            runtime=ctx.runtime, command="claude",
            timeout_seconds=ctx.cfg.get("agent_timeout_seconds", 3600),
            allowed_tools="Read,Edit,Write,Bash",
            max_output_tokens=ctx.cfg.get("agent_max_output_tokens", 64000),
            model=executor["model"],
            base_url=executor["base_url"],
            usage_observer=ctx.telemetry.record_usage,
            mounts=MountMap(rw=("proposer",)),
        )
    return ctx.self_executor_agent


def _run_self_review_round(ctx: RunContext, round_id: int) -> str:
    """Run one RSI self-review round: invoke the proposer in self mode, record the
    decision in reviews.jsonl, and write the next commitment back to state.json.
    On CHANGE (S3d) the Host also applies the self_change via the self-executor,
    smoke-tests the candidate, and adopts it if viable — filling the ledger's
    candidate_self_sha / viable / adopted. Returns the decision ("KEEP"|"CHANGE").
    Does NOT touch the task incumbent, prior_metrics, or history.jsonl — the caller
    ``continue``s past the task-round body."""
    payload = ctx.execution_backend.run_self_review(round_id=round_id)
    decision = payload.get("decision") or "KEEP"
    defer = payload.get("next_review_after_rounds")
    if not isinstance(defer, int) or isinstance(defer, bool) or defer < 1:
        defer = _DEFAULT_SELF_REVIEW_DEFER
    next_review_round = round_id + defer

    candidate_sha = viable = adopted = None
    if decision == "CHANGE" and payload.get("self_change"):
        tr = ctx.self_repo.transition(
            agent=_get_self_executor(ctx),
            self_change=payload["self_change"],
            run_dir=ctx.run_dir,
            label=f"self-exec r{round_id}")
        candidate_sha, viable, adopted = (
            tr.candidate_self_sha, tr.viable, tr.adopted)
        if tr.adopted:
            outcome = "adopted"
        elif tr.candidate_self_sha:
            outcome = f"rejected ({tr.detail})"
        else:
            outcome = f"no candidate ({tr.detail})"
        print(f"[{stamp()}] [rsi] self-change r{round_id + 1}: {outcome}"
              + (f" candidate={tr.candidate_self_sha[:10]}"
                 if tr.candidate_self_sha else ""), flush=True)

    ctx.self_repo.append_review(
        round_id, payload=payload, next_review_round=next_review_round,
        candidate_self_sha=candidate_sha, viable=viable, adopted=adopted)
    ctx.self_repo.update_commitment(next_self_review_round=next_review_round)
    return decision


def _summary(ctx: RunContext, run_dir_path: Path) -> dict:
    """Build the run summary dict and persist it as run_dir/summary.json
    (shared by the normal exit and the continue no-op exit)."""
    store, workspace = ctx.store, ctx.workspace
    history = store.history()
    schema = ctx.metrics_schema or {}
    obj_key = (schema.get("objective") or {}).get("key")
    # continue-no-op exits before the baseline eval runs; fall back to the
    # baseline metrics persisted in telemetry.json by the original session.
    baseline_metrics = ctx.baseline_metrics or (
        ctx.telemetry.plot_context().get("baseline_metrics")
        if ctx.telemetry else {}) or {}
    best = _best_candidate(history, schema) if history and obj_key else None
    baseline_sha = workspace.baseline_sha()
    summary = {
        "best_sha": best.get("sha") if best else None,
        "best_round": best.get("round") if best else None,
        "best_candidate": best.get("candidate") if best else None,
        "objective_key": obj_key,
        "best_objective": (best.get("metrics") or {}).get(obj_key) if best else None,
        "baseline_objective": baseline_metrics.get(obj_key),
        "baseline_sha": baseline_sha,
        "final_chain_sha": (history[-1].get("base_sha") if history
                            else baseline_sha),
        "rounds": len(history),
        "run_dir": str(run_dir_path),
        "repo": str(workspace.repo),
    }
    try:
        (run_dir_path / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError as exc:
        print(f"[{stamp()}] warning: could not write summary.json: {exc}",
              flush=True)
    return summary


def _refresh_progress_plot(store: Store, plot_context: dict | None = None) -> None:
    """Refresh the 2x3 overview only; detail images are drawn offline via
    scripts/plot_details.py."""
    try:
        history = store.history()
    except Exception as exc:
        print(f"[plot] warning: could not read {store.path}: {exc}", flush=True)
        return
    plot_mod.write_progress_png(
        store.run_dir, history, store.metrics_schema, plot_context,
    )


def _finalize_candidates(
    ctx: RunContext,
    candidates: tuple[CandidateResult, ...],
) -> tuple[CandidateResult, ...]:
    """Harness bookkeeping applied uniformly to every returned candidate,
    for both backends: ingest worker-reported usage into telemetry, then
    stamp each candidate with a persisted telemetry snapshot for its
    history row. Backends never touch telemetry themselves."""
    if ctx.telemetry is None:
        return candidates
    finalized = []
    for candidate in candidates:
        for usage in candidate.usage:
            ctx.telemetry.record_usage(usage)
        finalized.append(replace(
            candidate,
            usage=(),
            telemetry=ctx.telemetry.snapshot(persist=True),
        ))
    return tuple(finalized)


def _run_candidates(ctx: RunContext, proposals: list[str],
                    round_id: int, parent_sha: str,
                    ) -> tuple[CandidateResult, ...]:
    """Run one generation's proposal strings, possibly concurrently."""
    max_workers = min(ctx.cfg.get("max_workers", 1), max(1, len(proposals)))
    if max_workers <= 1 or len(proposals) <= 1:
        return tuple(
            _run_candidate_guarded(
                ctx, i, proposal, round_id, parent_sha)
            for i, proposal in enumerate(proposals)
        )
    results: list[CandidateResult | None] = [None] * len(proposals)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_candidate_guarded, ctx, i, proposal, round_id,
                        parent_sha): i
            for i, proposal in enumerate(proposals)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as exc:
                # Last-resort guard: candidate failures stay local to the candidate.
                print(f"[{stamp()}] candidate r{round_id}-c{i} worker failed: {exc}",
                      flush=True)
                results[i] = _candidate_failure(
                    i, proposals[i], f"candidate worker failed: {exc}",
                    parent_sha, round_id=round_id,
                    metrics_schema=ctx.metrics_schema)
    return tuple(r for r in results if r is not None)


def _run_candidate_guarded(
    ctx: RunContext,
    candidate_id: int,
    proposal: str,
    round_id: int,
    parent_sha: str,
) -> CandidateResult:
    try:
        return _run_one_candidate(
            ctx, candidate_id, proposal, round_id, parent_sha,
        )
    except Exception as exc:
        print(
            f"[{stamp()}] candidate r{round_id}-c{candidate_id} "
            f"worker failed: {exc}",
            flush=True,
        )
        return _candidate_failure(
            candidate_id, proposal, f"candidate worker failed: {exc}",
            parent_sha, round_id=round_id,
            metrics_schema=ctx.metrics_schema,
        )


def _deps_from_ctx(ctx: RunContext) -> candidate_worker.CandidateDeps:
    """Map the frontend's shared fixtures onto the worker dependency bundle;
    the local backend runs the exact same business code as a remote worker."""
    return candidate_worker.CandidateDeps(
        cfg=ctx.cfg, run_dir=ctx.run_dir, runtime=ctx.runtime,
        workspace=ctx.workspace, executor_agent=ctx.executor_agent,
        prompt_dir=ctx.prompt_dir, gate_lines=ctx.gate_lines,
    )


def _run_one_candidate(ctx: RunContext, candidate_id: int,
                       proposal: str,
                       round_id: int, parent_sha: str) -> CandidateResult:
    """LocalBackend's per-candidate path: the backend owns the worktree
    lifecycle; the business logic lives in candidate_worker.run_candidate."""
    worktree_id = f"{round_id}-c{candidate_id}"
    worktree = None
    try:
        worktree = ctx.workspace.add_worktree(worktree_id, parent_sha)
        spec = candidate_worker.CandidateSpec(
            round_id=round_id, candidate_id=candidate_id,
            parent_sha=parent_sha, proposal=proposal,
            run_dir=str(ctx.run_dir),
            worktree_path=str(worktree),
            prompt_dir=str(ctx.prompt_dir or ""),
        )
        return candidate_worker.run_candidate(_deps_from_ctx(ctx), spec)
    finally:
        if worktree is not None:
            ctx.workspace.remove_worktree(worktree_id)


def _candidate_failure(candidate_id: int, proposal: str,
                       reason: str, parent_sha: str, *, round_id: int = 0,
                       sha: str | None = None,
                       eval_block: str = "", eval_metrics: dict | None = None,
                       changed_paths: list[str] | None = None,
                       metrics_schema: dict | None = None) -> CandidateResult:
    spec = candidate_worker.CandidateSpec(
        round_id=round_id, candidate_id=candidate_id, parent_sha=parent_sha,
        proposal=proposal)
    return candidate_worker.candidate_failure(
        candidate_id, spec, reason, parent_sha, sha=sha,
        eval_block=eval_block, eval_metrics=eval_metrics,
        changed_paths=changed_paths, metrics_schema=metrics_schema)


def _print_round_performance(
    round_id: int,
    candidates: tuple[CandidateResult, ...],
    metrics_schema: dict,
    prior_metrics: dict | None,
) -> None:
    """Print the best gate-passing harness result against the round parent."""
    obj = metrics_schema["objective"]
    key = obj["key"]
    selection = select_candidate(
        candidates=candidates,
        objective_key=key,
        lower_is_better=obj["lower_is_better"],
        incumbent_value=None,
        require_improvement=False,
    )
    best = next(
        (
            candidate for candidate in candidates
            if candidate.candidate_id == selection.candidate_id
        ),
        None,
    )
    if best is None:
        result = f"{key}=unavailable (no eligible candidate)"
    else:
        value = best.metrics[key]
        delta = evals.objective_delta(
            value, (prior_metrics or {}).get(key), obj["lower_is_better"],
        )
        relative = "unavailable"
        if delta is not None:
            pct_change, _ = delta
            improvement = -pct_change if obj["lower_is_better"] else pct_change
            relative = f"{improvement:+.2f}%"
        result = f"{key}={value:g}, relative improvement={relative}"
    print(
        f"[{stamp()}] harness performance round {round_id + 1}: {result}",
        flush=True,
    )


def _resume_chain(history: list[dict],
                  baseline_sha: str) -> tuple[str, dict | None]:
    """Return the last selected SHA and record, skipping unselected tails."""
    for record in reversed(history):
        selected_sha = record.get("selected_sha")
        if selected_sha:
            selected = next((c for c in record.get("candidates") or []
                             if c.get("selected")), None)
            return selected_sha, selected or record
    return baseline_sha, None


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_inflight(run_dir: Path) -> dict | None:
    """Read run_dir/inflight_round.json, or None when no in-flight round is
    persisted. A corrupt/empty file is treated as absent so a half-written
    atomic file never blocks a resume."""
    path = run_dir / INFLIGHT_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "round_id" in data:
            return data
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[{stamp()}] warning: inflight_round.json unreadable ({exc}); "
              "ignoring", flush=True)
    return None


def _load_proposals(proposals: str | Path | list[str] | None) -> list[str] | None:
    """Resolve the `proposals` argument to a list of non-empty strings, or None.
    Accepts a list, or a YAML/JSON file holding a list (or {"proposals": [...]})."""
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
