from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from simpleloop.scheduling.contracts import (
    JobHandle,
    JobSpec,
    JobState,
    ResourceSpec,
    RetryPolicy,
)
from simpleloop.scheduling.envelope import WorkerRequest
from simpleloop.scheduling.hepjob import HEPJobConfig, HEPJobScheduler


def _config(**overrides):
    values = dict(
        schedd_name="scheduler@schedd11",
        collector="collector.example",
        accounting_group="JUNO.juno.default",
        accounting_group_user="alice",
        request_os="AlmaLinux9",
        ihep_group=None,
        submit_cmd="condor_submit",
        query_cmd="condor_q",
        remove_cmd="condor_rm",
    )
    values.update(overrides)
    return HEPJobConfig(**values)


def _job(tmp_path: Path) -> JobSpec:
    result = tmp_path / "result.json"
    return JobSpec(
        WorkerRequest("candidate", "r1-c0", {}, result),
        tmp_path / "manifest.json",
        result,
        tmp_path / "job.out",
        tmp_path / "job.err",
        ("/python path/python", "-m", "simpleloop.scheduling.worker",
         "--manifest", str(tmp_path / "manifest.json")),
        RetryPolicy(2, 100, 0),
        ResourceSpec(2, 4096, 'Machine == "node1"'),
    )


class Runner:
    def __init__(self):
        self.calls = []
        self.outputs = []

    def queue(self, returncode=0, stdout="", stderr=""):
        self.outputs.append(SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr,
        ))

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self.outputs.pop(0)


def test_submit_renders_worker_resources_and_target(tmp_path):
    runner = Runner()
    runner.queue(stdout="1 job(s) submitted to cluster 12345.\n")
    scheduler = HEPJobScheduler(_config(), runner=runner)

    handle = scheduler.submit(_job(tmp_path))

    assert handle == JobHandle("hepjob", "12345.0")
    assert runner.calls == [[
        "condor_submit", "-pool", "collector.example",
        "-name", "scheduler@schedd11", str(tmp_path / "job.sub"),
    ]]
    script = (tmp_path / "job.sh").read_text()
    assert "'/python path/python'" in script
    assert "simpleloop.scheduling.worker" in script
    submit = (tmp_path / "job.sub").read_text()
    assert "request_memory = 4096" in submit
    assert "request_cpus = 2" in submit
    assert 'Requirements = Machine == "node1"' in submit
    assert "accounting_group = JUNO.juno.default" in submit
    assert '+IHEP_RealGroup = "juno"' in submit


def test_submit_uses_explicit_ihep_group(tmp_path):
    runner = Runner()
    runner.queue(stdout="submitted to cluster 7")
    HEPJobScheduler(_config(ihep_group="special"), runner=runner).submit(_job(tmp_path))
    assert '+IHEP_RealGroup = "special"' in (tmp_path / "job.sub").read_text()


def test_inspect_maps_queue_states_and_missing_handle(tmp_path):
    runner = Runner()
    runner.queue(stdout="1 0 1\n2 0 2\n3 0 5\n")
    scheduler = HEPJobScheduler(_config(), runner=runner)
    handles = tuple(JobHandle("hepjob", f"{i}.0") for i in range(1, 5))

    observations = scheduler.inspect(handles)

    assert [item.state for item in observations] == [
        JobState.PENDING, JobState.RUNNING, JobState.FAILED, JobState.LOST,
    ]
    assert runner.calls[0][:5] == [
        "condor_q", "-pool", "collector.example", "-name", "scheduler@schedd11",
    ]


def test_failed_query_returns_unknown_for_every_handle():
    runner = Runner()
    runner.queue(returncode=1, stderr="schedd unavailable")
    scheduler = HEPJobScheduler(_config(), runner=runner)
    handles = (JobHandle("hepjob", "1.0"), JobHandle("hepjob", "2.0"))

    assert [item.state for item in scheduler.inspect(handles)] == [
        JobState.UNKNOWN, JobState.UNKNOWN,
    ]


def test_cancel_uses_configured_remove_and_target():
    runner = Runner()
    runner.queue()
    scheduler = HEPJobScheduler(_config(remove_cmd="remove"), runner=runner)

    scheduler.cancel(JobHandle("hepjob", "3.0"))

    assert runner.calls == [[
        "remove", "-pool", "collector.example", "-name", "scheduler@schedd11",
        "3.0",
    ]]
