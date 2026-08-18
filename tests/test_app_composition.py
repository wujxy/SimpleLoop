from __future__ import annotations

from simpleloop.app import _build_worker_jobs, _reconcile_inflight
from simpleloop.scheduling.envelope import WorkerResult, WorkerStatus, write_result
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


def test_reconcile_clears_stale_journal_for_committed_round(tmp_path):
    jobs = _build_worker_jobs(_config(), tmp_path, object())
    jobs.journal.begin("candidates", 3, {"payloads": []}, [])

    _reconcile_inflight(jobs, [{"round": 2}, {"round": 3}])

    assert jobs.inflight() is None


def test_reconcile_keeps_journal_for_uncommitted_round(tmp_path):
    jobs = _build_worker_jobs(_config(), tmp_path, object())
    jobs.journal.begin("candidates", 3, {"payloads": []}, [])

    _reconcile_inflight(jobs, [{"round": 2}])

    assert jobs.inflight().stage == "candidates"
    assert jobs.inflight().round_id == 3


def test_reconcile_leaves_rsi_stages_to_self_history(tmp_path):
    jobs = _build_worker_jobs(_config(), tmp_path, object())
    jobs.journal.begin("self_edit", 3, {"payload": {}}, [])

    _reconcile_inflight(jobs, [{"round": 3}])

    assert jobs.inflight().stage == "self_edit"


def test_reconcile_purges_failed_stage_results_and_drops_journal(tmp_path):
    # The interrupted stage recorded a protocol failure (worker exited
    # normally, payload outcome="error" — e.g. LLM 429/quota). Replaying it
    # would fail every future --continue with the same fossil error; the
    # stage must rebuild and retry instead.
    jobs = _build_worker_jobs(_config(), tmp_path, object())
    result_dir = tmp_path / "rounds" / "r3" / "lanes" / "l0"
    result_dir.mkdir(parents=True)
    write_result(result_dir / "result.json", WorkerResult(
        "proposer", "r3-l0", WorkerStatus.COMPLETED,
        {"outcome": "error", "abstain_reason": "429 quota exhausted"},
    ))
    jobs.journal.begin(
        "proposer", 3, {"payload": {"result_dir": str(result_dir)}}, [],
    )

    _reconcile_inflight(jobs, [{"round": 2}])

    assert jobs.inflight() is None
    assert not (result_dir / "result.json").exists()


def test_reconcile_keeps_journal_for_midflight_stage_without_results(tmp_path):
    # No result file at all: the run died with the job still in flight, and
    # the recorded workspace may still be alive — the designed resume path
    # must stay intact.
    jobs = _build_worker_jobs(_config(), tmp_path, object())
    jobs.journal.begin(
        "proposer", 3,
        {"payload": {"result_dir": str(tmp_path / "rounds" / "r3" / "l0")}},
        [],
    )

    _reconcile_inflight(jobs, [{"round": 2}])

    assert jobs.inflight().stage == "proposer"


def test_reconcile_keeps_journal_and_success_results(tmp_path):
    jobs = _build_worker_jobs(_config(), tmp_path, object())
    result_dir = tmp_path / "rounds" / "r3" / "lanes" / "l0"
    result_dir.mkdir(parents=True)
    write_result(result_dir / "result.json", WorkerResult(
        "proposer", "r3-l0", WorkerStatus.COMPLETED,
        {"outcome": "proposed", "proposals": [{"instruction": "x"}]},
    ))
    jobs.journal.begin(
        "proposer", 3, {"payload": {"result_dir": str(result_dir)}}, [],
    )

    _reconcile_inflight(jobs, [{"round": 2}])

    assert jobs.inflight().stage == "proposer"
    assert (result_dir / "result.json").exists()


def test_reconcile_purges_only_failed_candidates(tmp_path):
    jobs = _build_worker_jobs(_config(), tmp_path, object())
    kept = tmp_path / "rounds" / "r3" / "candidates" / "c0"
    failed = tmp_path / "rounds" / "r3" / "candidates" / "c1"
    for path in (kept, failed):
        path.mkdir(parents=True)
    write_result(kept / "result.json", WorkerResult(
        "candidate", "r3-c0", WorkerStatus.COMPLETED, {"ok": True},
    ))
    write_result(failed / "result.json", WorkerResult(
        "candidate", "r3-c1", WorkerStatus.FAILED, {}, error="eval crash",
    ))
    jobs.journal.begin("candidates", 3, {"payloads": [
        {"result_dir": str(kept)},
        {"result_dir": str(failed)},
    ]}, [])

    _reconcile_inflight(jobs, [{"round": 2}])

    assert jobs.inflight() is None
    assert (kept / "result.json").exists()
    assert not (failed / "result.json").exists()
