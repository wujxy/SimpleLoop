"""RSI jobs must complete against the real task-workspace provider.

Regression guard: self worktrees and the self repo live under the self body
store's own roots. The supervisor used to release every job workspace through
the task ``GitWorkspaceProvider``, which raises ``WorkspaceError`` for those
paths — crashing the whole run on the first RSI CHANGE decision and
crash-looping on ``--continue``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from simpleloop.persistence.journal import JobJournal
from simpleloop.rsi.models import SelfChange, SelfEditRequest, ViabilityRequest
from simpleloop.scheduling.contracts import (
    JobHandle, JobObservation, JobState, ResourceSpec, RetryPolicy,
)
from simpleloop.scheduling.envelope import WorkerResult, WorkerStatus, write_result
from simpleloop.scheduling.jobs import WorkerJobPolicy, WorkerJobs
from simpleloop.scheduling.rsi import (
    ScheduledSelfEditor,
    ScheduledViabilityChecker,
)
from simpleloop.scheduling.supervisor import JobSupervisor
from simpleloop.world import SourceWorkspace
from simpleloop.world.git import GitWorkspaceProvider


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()


def _make_task_provider(tmp_path: Path) -> tuple[GitWorkspaceProvider, str]:
    source = tmp_path / "source"
    _git("init", str(source))
    _git("-C", str(source), "config", "user.name", "Test")
    _git("-C", str(source), "config", "user.email", "test@example.invalid")
    (source / "a.py").write_text("old\n", encoding="utf-8")
    _git("-C", str(source), "add", "-A")
    _git("-C", str(source), "commit", "-m", "baseline")
    base = _git("-C", str(source), "rev-parse", "HEAD")
    provider = GitWorkspaceProvider(tmp_path / "run", source, "HEAD")
    assert provider.initialize() == base
    return provider, base


class _Telemetry:
    def __init__(self):
        self.records = []

    def record_usage(self, row):
        self.records.append(row)


class _CompletingScheduler:
    name = "fake"

    def __init__(self, result_payload):
        self.result_payload = result_payload
        self.submitted = []

    def submit(self, job):
        self.submitted.append(job)
        write_result(job.result_path, WorkerResult(
            job.request.kind,
            job.request.request_id,
            WorkerStatus.COMPLETED,
            self.result_payload,
        ))
        return JobHandle(self.name, str(len(self.submitted)))

    def inspect(self, handles):
        return tuple(
            JobObservation(handle, JobState.RUNNING) for handle in handles
        )

    def cancel(self, handle):
        pass


def _jobs(run_dir: Path, provider, result_payload):
    return WorkerJobs(
        scheduler=_CompletingScheduler(result_payload),
        supervisor=JobSupervisor(
            workspace_provider=provider,
            clock=lambda: 0.0,
            sleep=lambda _: None,
            poll_seconds=0,
        ),
        journal=JobJournal(run_dir / "inflight.json"),
        policy=WorkerJobPolicy(
            "python", RetryPolicy(1, 10, 0), ResourceSpec(),
        ),
    )


def test_self_edit_completes_and_keeps_self_worktree(tmp_path):
    provider, base = _make_task_provider(tmp_path)
    run_dir = tmp_path / "run"
    jobs = _jobs(run_dir, provider, {
        "self_edit": {"status": "EDITED", "output": "done"},
    })
    self_worktree = run_dir / "self" / "worktrees" / "rself-4"
    self_worktree.mkdir(parents=True)
    editor = ScheduledSelfEditor(
        run_dir=run_dir, jobs=jobs, telemetry=_Telemetry(), prompt_dir=None,
    )

    result = editor.edit(SelfEditRequest(
        4,
        SelfChange("prompt", "broaden", "edit charter"),
        SourceWorkspace("rself-4", self_worktree, base),
    ))

    assert result.status == "EDITED"
    assert self_worktree.exists()


def test_viability_completes_and_keeps_self_repo(tmp_path):
    provider, base = _make_task_provider(tmp_path)
    run_dir = tmp_path / "run"
    (run_dir / "config.resolved.json").write_text(
        json.dumps({"goal": "g"}), encoding="utf-8",
    )
    jobs = _jobs(run_dir, provider, {
        "status": "COMPLETED", "outcome": "abstain", "proposals": [],
    })
    self_repo = run_dir / "self" / "repo"
    self_repo.mkdir(parents=True)
    (self_repo / "proposer").mkdir()
    checker = ScheduledViabilityChecker(
        run_dir=run_dir, jobs=jobs, telemetry=_Telemetry(),
    )

    result = checker.check(ViabilityRequest(4, "s1", self_repo))

    assert result.viable is True
    assert self_repo.is_dir()
