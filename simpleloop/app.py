"""Application composition root for one SimpleLoop run.

Configuration is interpreted here exactly once. Domain pipelines receive
small typed ports and never see a context/config bundle.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

from . import config as config_mod
from .harness import evals
from .harness.store import Store
from .loop import LoopRequest, LoopState, run_loop
from .persistence.journal import JobJournal
from .persistence.round_artifacts import RoundArtifacts
from .processes import run_signal_handlers
from .reporting import plot as plot_mod
from .reporting.summary import write_summary
from .reporting.telemetry import RunTelemetry
from .roles.agent import Agent
from .round import RoundRequest, SelectionPolicy, run_round
from .scheduling.contracts import ResourceSpec, RetryPolicy
from .scheduling.hepjob import HEPJobConfig, HEPJobScheduler
from .scheduling.jobs import WorkerJobPolicy, WorkerJobs
from .scheduling.local import LocalScheduler
from .scheduling.rsi import ScheduledSelfReview, ScheduledViability
from .scheduling.supervisor import JobSupervisor
from .scheduling.task import (
    BaselineRequest, ScheduledBaseline, ScheduledCandidates, ScheduledProposer,
)
from .self_repo import LegacyRsiRunner, SelfRepo, check_viability
from .stages.evaluator import BaselineAcceptanceError
from .stages.proposer import StaticProposer
from .stages.selector import select_candidate
from .world import (
    ApptainerSandbox, ProcessRequest, SandboxPreflightError, SandboxSpec,
    SourceWorkspace, WorkspaceSpec, WorldBuilder, executor_environment,
    executor_world_spec, forwarded_payload_env,
)
from .world.git import GitWorkspaceProvider


class RunLockError(RuntimeError):
    pass


class _TaskRounds:
    def __init__(self, *, proposer, candidates, recorder):
        self.proposer = proposer
        self.candidates = candidates
        self.recorder = recorder

    def run(self, request: RoundRequest):
        return run_round(
            request,
            proposer=self.proposer,
            candidates=self.candidates,
            recorder=self.recorder,
        )


class _RoundObserver:
    def __init__(self, *, store, telemetry, metrics_schema, prior_metrics):
        self.store = store
        self.telemetry = telemetry
        self.metrics_schema = metrics_schema
        self.prior_metrics = dict(prior_metrics)

    def round_committed(self, result) -> None:
        _print_round_performance(
            result.round_id, result.candidates,
            self.metrics_schema, self.prior_metrics,
        )
        winner = next((
            candidate for candidate in result.candidates
            if candidate.candidate_id == result.selection.candidate_id
        ), None)
        if winner is not None:
            self.prior_metrics = dict(winner.metrics)
            print(
                f"[{stamp()}] selected candidate r{result.round_id}-"
                f"c{winner.candidate_id}: {result.next_sha[:10]}", flush=True,
            )
        _refresh_progress_plot(self.store, self.telemetry.plot_context())


def run(
    config_path: str | Path,
    run_dir: str | Path,
    proposals: str | Path | list[str] | None = None,
    continue_run: bool = False,
    target_rounds: int | None = None,
    prompt_dir: str | Path | None = None,
) -> dict:
    cfg = config_mod.load(config_path)
    run_dir_path = Path(run_dir).resolve()
    run_dir_path.mkdir(parents=True, exist_ok=True)
    history_path = run_dir_path / "history.jsonl"
    if (
        not continue_run and history_path.exists()
        and history_path.stat().st_size > 0
    ):
        raise ValueError(
            f"run-dir {run_dir} already contains existing runs "
            "(history.jsonl found). Use --continue to resume."
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


def _run_locked(
    cfg: dict,
    run_dir: Path,
    proposals: str | Path | list[str] | None,
    continue_run: bool,
    target_rounds: int | None = None,
    prompt_dir: Path | None = None,
) -> dict:
    static = _load_proposals(proposals)
    stop_round = _stop_round(cfg, static, continue_run, target_rounds)

    telemetry = RunTelemetry(run_dir, resume=continue_run)
    sandbox = ApptainerSandbox()
    sandbox_spec = _executor_sandbox_spec(cfg)
    sandbox.preflight(sandbox_spec)
    _assert_executor_ready(cfg)
    world_builder = WorldBuilder(sandbox)
    workspace = GitWorkspaceProvider(
        run_dir=run_dir,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
    )
    workspace.initialize()
    store = Store(
        run_dir, metrics_schema=cfg["metrics"],
        history_eval_cap=cfg.get("eval_history_cap_chars", 6000),
    )
    self_repo = SelfRepo(run_dir)
    self_repo.setup(resume=continue_run)
    _seed_rsi_commitment(cfg, self_repo, continue_run)
    _preflight_executor(cfg, workspace, world_builder, sandbox_spec)

    jobs = _build_worker_jobs(cfg, run_dir, workspace)
    baseline_jobs = _build_worker_jobs(
        cfg, run_dir, workspace,
        journal_path=run_dir / "baseline" / "inflight.json",
    )
    baseline = ScheduledBaseline(
        run_dir=run_dir, workspace=workspace, jobs=baseline_jobs,
        telemetry=telemetry, metrics_schema=cfg["metrics"],
    )
    state_and_baseline = _starting_state(
        continue_run=continue_run,
        stop_round=stop_round,
        history=store.history(),
        baseline_sha=workspace.baseline_sha(),
        last_self_review=self_repo.last_review_round(),
        baseline=baseline,
        telemetry=telemetry,
    )
    if state_and_baseline is None:
        prior_baseline = telemetry.plot_context().get("baseline_metrics") or {}
        _refresh_progress_plot(store, telemetry.plot_context())
        return write_summary(
            run_dir=run_dir, store=store, workspace=workspace,
            metrics_schema=cfg["metrics"], baseline_metrics=prior_baseline,
        )
    state, baseline_metrics = state_and_baseline

    proposer = (
        StaticProposer(static)
        if static is not None
        else ScheduledProposer(
            run_dir=run_dir, workspace=workspace, jobs=jobs,
            telemetry=telemetry,
            proposal_slots=int(cfg.get("candidates_per_round", 1)),
            scientist_steps=int(cfg.get("scientist_steps", 200)),
            prompt_dir=prompt_dir,
        )
    )
    candidates = ScheduledCandidates(
        run_dir=run_dir, workspace=workspace, jobs=jobs,
        telemetry=telemetry, max_parallel=int(cfg.get("max_workers", 1)),
        prompt_dir=prompt_dir,
    )
    reviewer = ScheduledSelfReview(
        run_dir=run_dir, jobs=jobs, telemetry=telemetry,
        scientist_steps=int(cfg.get("scientist_steps", 200)),
        prompt_dir=prompt_dir,
    )
    viability = ScheduledViability(jobs=jobs, telemetry=telemetry)
    rsi = LegacyRsiRunner(
        self_repo=self_repo,
        reviewer=reviewer,
        executor=_self_executor_factory(cfg, world_builder, telemetry),
        viability=lambda candidate, candidate_run_dir: check_viability(
            candidate, candidate_run_dir, execute=viability.check,
        ),
        run_dir=run_dir,
    )
    observer = _RoundObserver(
        store=store, telemetry=telemetry, metrics_schema=cfg["metrics"],
        prior_metrics=state.incumbent_metrics,
    )
    objective = cfg["metrics"]["objective"]
    result = run_loop(
        LoopRequest(
            str(cfg["goal"]), stop_round, state,
            SelectionPolicy(
                str(objective["key"]), bool(objective["lower_is_better"]),
                require_improvement=static is None,
            ),
        ),
        rounds=_TaskRounds(
            proposer=proposer, candidates=candidates,
            recorder=RoundArtifacts(run_dir),
        ),
        rsi=rsi,
        history=store,
        checkpoint=jobs,
        observer=observer,
    )
    if result.interrupted:
        print(f"[{stamp()}] {result.interruption}", flush=True)
    summary = write_summary(
        run_dir=run_dir, store=store, workspace=workspace,
        metrics_schema=cfg["metrics"], baseline_metrics=baseline_metrics,
    )
    print(f"[{stamp()}] done. best={summary['best_sha']}", flush=True)
    return summary


def _starting_state(
    *, continue_run: bool, stop_round: int, history: list[dict],
    baseline_sha: str, last_self_review: int | None, baseline, telemetry,
):
    if continue_run:
        if not history:
            raise ValueError(
                "--continue: run directory has no history.jsonl rounds"
            )
        last_task = history[-1].get("round", len(history) - 1)
        next_round = max(
            last_task,
            last_self_review if last_self_review is not None else -1,
        ) + 1
        if next_round >= stop_round:
            return None
        incumbent_sha, selected = _resume_chain(history, baseline_sha)
    else:
        next_round, incumbent_sha, selected = 0, baseline_sha, None
    baseline_result = baseline.evaluate(BaselineRequest(baseline_sha))
    baseline_metrics = dict(baseline_result.metrics)
    if not continue_run:
        telemetry.set_baseline(baseline_metrics)
    incumbent_metrics = (
        dict(selected.get("metrics") or {}) if selected else baseline_metrics
    )
    return LoopState(next_round, incumbent_sha, incumbent_metrics), baseline_metrics


def _build_worker_jobs(
    cfg: Mapping[str, object], run_dir: Path, workspace, *,
    journal_path: str | Path | None = None,
):
    backend = str(cfg.get("execution_backend") or "local").strip().lower()
    hep = cfg.get("hepjob") or {}
    if backend == "local":
        scheduler, poll_seconds, python = LocalScheduler(), 0.2, sys.executable
    elif backend == "hepjob":
        scheduler = HEPJobScheduler(HEPJobConfig(
            schedd_name=str(hep["schedd_name"]),
            collector=str(hep["collector"]) if hep.get("collector") else None,
            accounting_group=str(hep["accounting_group"]),
            accounting_group_user=str(hep["accounting_group_user"]),
            request_os=str(hep.get("request_os") or "AlmaLinux9"),
            ihep_group=str(hep["ihep_group"]) if hep.get("ihep_group") else None,
            submit_cmd=str(hep.get("submit_cmd") or HEPJobConfig.submit_cmd),
            query_cmd=str(hep.get("query_cmd") or HEPJobConfig.query_cmd),
            remove_cmd=str(hep.get("remove_cmd") or HEPJobConfig.remove_cmd),
            environment_script=_write_job_environment(run_dir),
        ))
        poll_seconds = float(hep.get("poll_seconds", 30))
        python = str(hep.get("python_executable") or sys.executable)
    else:
        raise ValueError(
            f"execution.backend: unknown backend {backend!r} "
            "(expected local|hepjob)"
        )
    return WorkerJobs(
        scheduler=scheduler,
        supervisor=JobSupervisor(
            workspace_provider=workspace, poll_seconds=poll_seconds,
        ),
        journal=JobJournal(journal_path or run_dir / "inflight.json"),
        policy=WorkerJobPolicy(
            python,
            RetryPolicy(
                int(hep.get("max_attempts", 2)),
                int(hep.get("run_timeout_seconds", 21600)),
                int(hep.get("disappearance_grace_seconds", 120)),
            ),
            ResourceSpec(
                int(hep.get("cpus", 1)), int(hep.get("memory_mb", 0)),
                _requirements(cfg),
            ),
        ),
    )


def _executor_sandbox_spec(cfg: Mapping[str, object]) -> SandboxSpec:
    executor = cfg["roles"]["executor"]
    return SandboxSpec(
        Path(cfg["runtime_image"]),
        executor_environment(
            base_url=executor.get("base_url"),
            max_output_tokens=int(cfg.get("agent_max_output_tokens", 64000)),
        ),
        True,
    )


def _preflight_executor(cfg, workspace, world_builder, sandbox_spec) -> None:
    worktree = workspace.create(WorkspaceSpec(
        "executor-preflight", workspace.baseline_sha(),
    ))
    try:
        world = world_builder.build(
            worktree,
            sandbox_spec,
            executor_world_spec(
                cfg.get("editable_paths", ()), cfg.get("read_only_binds", ()),
            ),
        )
        result = world.run(ProcessRequest(
            ("bash", "-c", "test \"$PWD\" = /work && command -v git >/dev/null && command -v claude >/dev/null"),
            PurePosixPath("/work"), 60, label="executor-preflight",
        ))
        if result.exit_code or result.timed_out:
            raise SandboxPreflightError(
                "executor preflight failed: "
                + (result.stderr or result.stdout)[:2000]
            )
    finally:
        workspace.remove(worktree)


def _self_executor_factory(cfg, world_builder, telemetry):
    def build(worktree: Path):
        workspace = SourceWorkspace("self-change", worktree, "")
        world = world_builder.build(
            workspace,
            _executor_sandbox_spec(cfg),
            executor_world_spec(("proposer",)),
        )
        executor = cfg["roles"]["executor"]
        return Agent(
            world=world, command="claude",
            timeout_seconds=cfg.get("agent_timeout_seconds", 3600),
            allowed_tools="Read,Edit,Write,Bash", model=executor["model"],
            usage_observer=telemetry.record_usage,
        )
    return build


def _stop_round(cfg, static, continue_run, target_rounds):
    if static is not None and continue_run:
        raise ValueError("--continue cannot be combined with --proposals")
    if static is not None:
        if target_rounds is not None:
            raise ValueError("target_rounds cannot be combined with static proposals")
        return len(static)
    if cfg.get("roles", {}).get("researcher") is None:
        raise config_mod.ConfigError(
            "roles.researcher: required for agent-driven runs"
        )
    if target_rounds is None:
        return int(cfg["max_rounds"])
    if (
        not isinstance(target_rounds, int) or isinstance(target_rounds, bool)
        or not 1 <= target_rounds <= cfg["max_rounds"]
    ):
        raise ValueError(
            "target_rounds must be between 1 and configured max_rounds"
        )
    return target_rounds


def _seed_rsi_commitment(cfg, self_repo, resume):
    if resume:
        return
    first = (cfg.get("rsi") or {}).get("first_self_review_round")
    if (
        isinstance(first, int) and not isinstance(first, bool) and first >= 0
        and self_repo.next_self_review_round is None
    ):
        self_repo.update_commitment(next_self_review_round=first)


def _assert_executor_ready(cfg: Mapping[str, object]) -> None:
    executor = (cfg.get("roles") or {}).get("executor")
    if not executor or not str(executor.get("base_url", "")).strip():
        raise config_mod.ConfigError(
            "roles.executor.base_url: required for candidate execution"
        )
    if not (
        os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    ):
        raise config_mod.ConfigError(
            "ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) must be exported"
        )


def _print_round_performance(
    round_id, candidates, metrics_schema, prior_metrics,
) -> None:
    objective = metrics_schema["objective"]
    key = objective["key"]
    selection = select_candidate(
        candidates=candidates, objective_key=key,
        lower_is_better=objective["lower_is_better"],
        incumbent_value=None, require_improvement=False,
    )
    best = next((
        candidate for candidate in candidates
        if candidate.candidate_id == selection.candidate_id
    ), None)
    if best is None:
        text = f"{key}=unavailable (no eligible candidate)"
    else:
        value = best.metrics[key]
        delta = evals.objective_delta(
            value, (prior_metrics or {}).get(key),
            objective["lower_is_better"],
        )
        relative = "unavailable"
        if delta is not None:
            change, _ = delta
            improvement = -change if objective["lower_is_better"] else change
            relative = f"{improvement:+.2f}%"
        text = f"{key}={value:g}, relative improvement={relative}"
    print(f"[{stamp()}] harness performance round {round_id + 1}: {text}", flush=True)


def _refresh_progress_plot(store, plot_context=None) -> None:
    try:
        history = store.history()
    except Exception as exc:
        print(f"[plot] warning: could not read {store.path}: {exc}", flush=True)
        return
    plot_mod.write_progress_png(
        store.run_dir, history, store.metrics_schema, plot_context,
    )


def _resume_chain(history: list[dict], baseline_sha: str):
    for record in reversed(history):
        selected_sha = record.get("selected_sha")
        if selected_sha:
            selected = next((
                candidate for candidate in record.get("candidates") or ()
                if candidate.get("selected")
            ), None)
            return selected_sha, selected or record
    return baseline_sha, None


def _requirements(cfg: Mapping[str, object]) -> str | None:
    hep = cfg.get("hepjob") or {}
    parts = []
    if hep.get("cpu_model"):
        parts.append(config_mod._CPU_MODEL_REQUIREMENTS[str(hep["cpu_model"])])
    if hep.get("machine_constraint"):
        parts.append(str(hep["machine_constraint"]))
    return " && ".join(parts) or None


def _write_job_environment(run_dir: Path) -> Path:
    path = run_dir / "job_env.sh"
    package_root = Path(__file__).resolve().parent.parent
    lines = ["# generated by SimpleLoop scheduling composition"]
    for key, value in sorted(forwarded_payload_env().items()):
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.append(
        f"export PYTHONPATH={shlex.quote(str(package_root))}"
        '"${PYTHONPATH:+:$PYTHONPATH}"'
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _load_proposals(proposals):
    if proposals is None:
        return None
    if isinstance(proposals, list):
        raw = proposals
    else:
        path = Path(proposals).expanduser()
        text = path.read_text(encoding="utf-8")
        document = (
            json.loads(text) if path.suffix.lower() == ".json"
            else yaml.safe_load(text)
        )
        raw = (
            document["proposals"]
            if isinstance(document, dict)
            and isinstance(document.get("proposals"), list)
            else document
        )
        if not isinstance(raw, list):
            raise ValueError(f"proposals file {path}: expected a list of strings")
    result = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"proposals[{index}]: must be a non-empty string")
        result.append(item.strip())
    if not result:
        raise ValueError("proposals: file/list is empty — nothing to run")
    return result


def _write_config_snapshot(cfg, config_path, run_dir: Path) -> None:
    (run_dir / config_mod.RESOLVED_SNAPSHOT_NAME).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    source = Path(config_path).expanduser().resolve()
    original = run_dir / f"config.orig{source.suffix}"
    if source.is_file() and not original.exists():
        shutil.copyfile(source, original)


def _acquire_run_lock(run_dir: Path) -> int | None:
    path = run_dir / ".lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RunLockError(f"run_dir {run_dir} is locked by another simpleloop run")
    except OSError:
        os.close(fd)
        return None
    info = json.dumps({
        "pid": os.getpid(), "host": socket.gethostname(), "started_at": stamp(),
    })
    os.ftruncate(fd, 0)
    os.pwrite(fd, info.encode("utf-8"), 0)
    return fd


def _release_run_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
