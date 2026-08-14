from __future__ import annotations

from simpleloop.app import _build_worker_jobs
from simpleloop.scheduling.hepjob import HEPJobScheduler
from simpleloop.scheduling.local import LocalScheduler


def _config(backend="local"):
    return {
        "execution_backend": backend,
        "hepjob": {
            "max_attempts": 2,
            "run_timeout_seconds": 100,
            "disappearance_grace_seconds": 0,
            "poll_seconds": 5,
            "cpus": 1,
            "memory_mb": 100,
            "python_executable": "python",
            "schedd_name": "schedd",
            "collector": "collector",
            "accounting_group": "JUNO.juno.default",
            "accounting_group_user": "alice",
            "request_os": "AlmaLinux9",
            "ihep_group": None,
            "submit_cmd": "condor_submit",
            "query_cmd": "condor_q",
            "remove_cmd": "condor_rm",
        },
    }


def test_composition_selects_local_scheduler(tmp_path):
    jobs = _build_worker_jobs(_config(), tmp_path, object())

    assert isinstance(jobs.scheduler, LocalScheduler)


def test_composition_selects_hepjob_and_writes_environment(tmp_path):
    jobs = _build_worker_jobs(_config("hepjob"), tmp_path, object())

    assert isinstance(jobs.scheduler, HEPJobScheduler)
    assert jobs.scheduler.config.environment_script.is_file()


def test_baseline_journal_cannot_clear_resumable_task_checkpoint(tmp_path):
    task_jobs = _build_worker_jobs(_config(), tmp_path, object())
    baseline_jobs = _build_worker_jobs(
        _config(), tmp_path, object(),
        journal_path=tmp_path / "baseline" / "inflight.json",
    )
    task_jobs.journal.begin("candidates", 2, {"payloads": []}, [])

    baseline_jobs.clear()

    assert task_jobs.inflight().stage == "candidates"
