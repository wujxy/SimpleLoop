from __future__ import annotations

import sys
import time
from pathlib import Path

from simpleloop.scheduling.contracts import (
    JobHandle,
    JobSpec,
    JobState,
    ResourceSpec,
    RetryPolicy,
)
from simpleloop.scheduling.envelope import WorkerRequest
from simpleloop.scheduling.local import LocalScheduler


def _job(tmp_path: Path, argv: tuple[str, ...]) -> JobSpec:
    result = tmp_path / "result.json"
    return JobSpec(
        WorkerRequest("candidate", "job", {}, result),
        tmp_path / "manifest.json",
        result,
        tmp_path / "stdout.log",
        tmp_path / "stderr.log",
        argv,
        RetryPolicy(1, 10, 0),
        ResourceSpec(),
    )


def _wait_terminal(scheduler, handle, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = scheduler.inspect((handle,))[0]
        if observed.state is not JobState.RUNNING:
            return observed
        time.sleep(0.01)
    raise AssertionError("local process did not terminate")


def test_local_scheduler_runs_shell_free_argv(tmp_path):
    marker = tmp_path / "should-not-exist"
    scheduler = LocalScheduler()
    job = _job(tmp_path, (
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1])",
        f"literal;touch {marker}",
    ))

    handle = scheduler.submit(job)
    observed = _wait_terminal(scheduler, handle)

    assert observed.state is JobState.SUCCEEDED
    assert job.stdout_path.read_text().strip() == f"literal;touch {marker}"
    assert not marker.exists()


def test_local_scheduler_reports_and_cancels_process_group(tmp_path):
    scheduler = LocalScheduler(terminate_grace_seconds=0.1)
    job = _job(tmp_path, (sys.executable, "-c", "import time; time.sleep(30)"))
    handle = scheduler.submit(job)

    assert scheduler.inspect((handle,))[0].state is JobState.RUNNING
    scheduler.cancel(handle)

    assert _wait_terminal(scheduler, handle).state is JobState.FAILED


def test_local_scheduler_treats_restored_handle_as_lost(tmp_path):
    first = LocalScheduler()
    job = _job(tmp_path, (sys.executable, "-c", "import time; time.sleep(30)"))
    handle = first.submit(job)
    restored = LocalScheduler(terminate_grace_seconds=0.1)
    try:
        assert restored.inspect((handle,))[0].state is JobState.LOST
        restored.cancel(handle)
    finally:
        first.cancel(handle)


def test_local_scheduler_captures_both_streams(tmp_path):
    scheduler = LocalScheduler()
    job = _job(tmp_path, (
        sys.executable,
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr)",
    ))

    handle = scheduler.submit(job)
    assert _wait_terminal(scheduler, handle).state is JobState.SUCCEEDED
    assert job.stdout_path.read_text().strip() == "out"
    assert job.stderr_path.read_text().strip() == "err"
