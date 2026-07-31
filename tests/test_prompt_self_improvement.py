from __future__ import annotations

from pathlib import Path
import fcntl
import json
import inspect

import pytest
import yaml

from simpleloop import cli, config
from simpleloop import loop
from simpleloop.self_improvement.gate import OptimizerReport, PromptGate
from simpleloop.self_improvement.history import PromptHistory
from simpleloop.self_improvement.optimizer import MetaOptimizer
from simpleloop.self_improvement import supervisor
from simpleloop.prompts import PROMPT_NAMES, load_semantic


def _task_file(tmp_path: Path, block=None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    raw = {
        "kind": "task",
        "task": {"goal": "faster"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 5},
        "runtime": {"image": str(image)},
        "source": {"path": str(repo)},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [],
            },
        },
    }
    if block is not None:
        raw["self_improvement"] = block
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _enabled(**overrides):
    return {
        "interval_rounds": 2,
        **overrides,
    }


def _set_max_rounds(task: Path, rounds: int) -> None:
    raw = yaml.safe_load(task.read_text())
    raw["loop"]["max_rounds"] = rounds
    task.write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_self_improvement_defaults_disabled(tmp_path: Path):
    cfg = config.load(_task_file(tmp_path))
    assert cfg["self_improvement"] is None


def test_self_improvement_resolves_minimal_block(tmp_path: Path):
    cfg = config.load(_task_file(tmp_path, _enabled()))
    assert cfg["self_improvement"] == {"interval_rounds": 2}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"interval_rounds": 0}, "interval_rounds"),
        ({"enabled": True}, "unknown"),
        ({"max_prompt_chars": 30000}, "unknown"),
        ({"optimizer_command": "claude"}, "unknown"),
        ({"prompt_dir": "prompts"}, "unknown"),
        ({"history_dir": "history"}, "unknown"),
        ({"unknown": True}, "unknown"),
    ],
)
def test_self_improvement_rejects_invalid_values(
    tmp_path: Path, overrides: dict, message: str,
):
    with pytest.raises(config.ConfigError, match=message):
        config.load(_task_file(tmp_path, _enabled(**overrides)))


def test_old_prompt_self_improvement_name_is_rejected(tmp_path: Path):
    task = _task_file(tmp_path)
    raw = yaml.safe_load(task.read_text())
    raw["prompt_self_improvement"] = {"enabled": True}
    task.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(config.ConfigError, match="unknown top-level"):
        config.load(task)


def test_history_initializes_new_v000_from_package_prompts(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    assert history.initialize() == "v000"
    for role in PROMPT_NAMES:
        expected = load_semantic(role) + "\n"
        assert (history.prompt_dir / f"{role}.md").read_text() == expected
        assert (history.history_dir / "v000" / f"{role}.md").read_text() == expected
    assert history.state["active_version"] == "v000"


def test_history_initialization_replaces_orphan_v000(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    orphan = history.history_dir / "v000"
    orphan.mkdir(parents=True)
    (orphan / "partial").write_text("interrupted initialization")

    assert history.initialize() == "v000"

    assert not (orphan / "partial").exists()
    assert history.state == {"active_version": "v000", "inflight": None}


def test_history_detects_changes_snapshots_and_restores(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    proposer = history.prompt_dir / "proposer.md"
    proposer.write_text("rewritten proposer\n", encoding="utf-8")
    assert history.has_changes("v000")

    version = history.snapshot(2, OptimizerReport(
        diagnosis="anchored", evidence=["r0c0"],
    ))
    assert version == "v001"
    assert (history.history_dir / "v001/proposer.md").read_text() == "rewritten proposer\n"

    proposer.write_text("partial", encoding="utf-8")
    history.restore("v001")
    assert proposer.read_text(encoding="utf-8") == "rewritten proposer\n"


def test_history_records_idempotent_trigger_and_recovers_inflight(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    assert history.mark_inflight("run-a", 2)
    assert not history.mark_inflight("run-a", 2)
    (history.prompt_dir / "proposer.md").write_text("partial")

    recovered = history.recover_inflight()
    assert recovered == {"run_id": "run-a", "trigger_round": 2, "parent": "v000"}
    assert "You are the PROPOSER" in (history.prompt_dir / "proposer.md").read_text()
    assert history.events()[-1]["status"] == "interrupted"


def test_history_rolls_back_snapshot_created_by_interrupted_trigger(
    tmp_path: Path,
):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    history.mark_inflight("run-a", 2)
    (history.prompt_dir / "proposer.md").write_text("interrupted rewrite")
    history.snapshot(2, OptimizerReport(
        diagnosis="test crash window", evidence=[],
    ))
    assert history.state["active_version"] == "v001"

    history.recover_inflight()

    assert history.state["active_version"] == "v000"
    assert not (history.history_dir / "v001").exists()
    assert "You are the PROPOSER" in (history.prompt_dir / "proposer.md").read_text()


def test_history_preserves_completed_trigger_with_stale_inflight(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    history.mark_inflight("run-a", 2)
    proposer = history.prompt_dir / "proposer.md"
    proposer.write_text("accepted rewrite")
    history.snapshot(2, OptimizerReport(
        diagnosis="accepted", evidence=[],
    ))
    history.record_event({
        "run_id": "run-a", "trigger_round": 2, "parent": "v000",
        "status": "accepted", "result_version": "v001",
    })

    history.recover_inflight()

    assert history.state == {"active_version": "v001", "inflight": None}
    assert proposer.read_text() == "accepted rewrite"
    assert [event["status"] for event in history.events()] == ["accepted"]


def _write_report(prompt_dir: Path, **overrides) -> Path:
    data = {
        "diagnosis": "role drift",
        "evidence": ["r0c0"],
        **overrides,
    }
    path = prompt_dir / "optimizer_report.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_report_parser_is_exact_and_gate_allows_role_rewrite(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    (history.prompt_dir / "proposer.md").write_text("wholly new semantics")
    report_path = _write_report(history.prompt_dir)

    report = OptimizerReport.load(report_path)
    assert report.diagnosis == "role drift"
    assert PromptGate().check(history.prompt_dir) == []


def test_gate_rejects_changed_meta_core(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    meta = history.prompt_dir / "meta_optimizer.md"
    meta.write_text(meta.read_text().replace("META OPTIMIZER", "TASK SOLVER"))
    OptimizerReport.load(_write_report(history.prompt_dir))

    errors = PromptGate().check(history.prompt_dir)
    assert any("META_IDENTITY_CORE" in error for error in errors)


def test_gate_rejects_symlink_and_restore_does_not_write_through_it(
    tmp_path: Path,
):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    outside = tmp_path / "outside.md"
    outside.write_text("do not change")
    proposer = history.prompt_dir / "proposer.md"
    proposer.unlink()
    proposer.symlink_to(outside)
    OptimizerReport.load(_write_report(history.prompt_dir))

    errors = PromptGate().check(history.prompt_dir)
    history.restore("v000")

    assert any("symbolic link" in error for error in errors)
    assert outside.read_text() == "do not change"
    assert proposer.is_file() and not proposer.is_symlink()


def test_report_rejects_unknown_fields(tmp_path: Path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    path = _write_report(prompt_dir, unknown=True)
    with pytest.raises(ValueError, match="exactly"):
        OptimizerReport.load(path)


def test_report_rejects_empty_diagnosis(tmp_path: Path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    path = _write_report(prompt_dir, diagnosis="")
    with pytest.raises(ValueError, match="diagnosis"):
        OptimizerReport.load(path)


def test_gate_uses_internal_prompt_length_limit(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    (history.prompt_dir / "proposer.md").write_text("x" * 30001)

    assert any(
        "exceeds 30000" in error
        for error in PromptGate().check(history.prompt_dir)
    )


def test_optimizer_receives_absolute_read_write_boundaries(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()

    class CapturingAgent:
        prompt = ""
        cwd = None

        def run_text(self, prompt, *, cwd, **_kwargs):
            self.prompt = prompt
            self.cwd = cwd
            return ""

    fake = CapturingAgent()
    optimizer = MetaOptimizer(
        60, 8000, agent_factory=lambda **_kwargs: fake,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    optimizer.run(
        run_dir=run_dir, prompt_dir=history.prompt_dir,
        history_dir=history.history_dir, source_dir=source_dir,
        goal="faster", gate_block="CORRECTNESS",
    )

    assert str(run_dir.resolve()) in fake.prompt
    assert str(history.history_dir.resolve()) in fake.prompt
    assert str(source_dir.resolve()) in fake.prompt
    assert "Fixed artifacts:" in fake.prompt
    assert "optimizer_report.yaml" in fake.prompt
    assert fake.cwd == history.prompt_dir.resolve()


def _write_rounds(run_dir: Path, target: int) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "history.jsonl"
    existing = path.read_text().splitlines() if path.exists() else []
    with path.open("a", encoding="utf-8") as stream:
        for round_id in range(len(existing), target):
            stream.write(json.dumps({"round": round_id}) + "\n")


def test_artifact_loop_exposes_target_rounds_override():
    assert "target_rounds" in inspect.signature(loop.run).parameters
    assert "prompt_dir" in inspect.signature(loop.run).parameters


def test_supervisor_stops_each_segment_before_optimizer(tmp_path: Path):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    run_dir = tmp_path / "run"
    calls = []

    def artifact_runner(
        _config, _run_dir, *, target_rounds, prompt_dir=None, **_kwargs,
    ):
        assert Path(prompt_dir) == run_dir / "self_improvement" / "prompts"
        calls.append(("artifact", target_rounds))
        _write_rounds(run_dir, target_rounds)
        return {"rounds": target_rounds}

    class NoChangeOptimizer:
        def run(self, *, prompt_dir, **_kwargs):
            calls.append(("optimizer", len((run_dir / "history.jsonl").read_text().splitlines())))
            _write_report(Path(prompt_dir))

    summary = supervisor.run(
        task, run_dir, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: NoChangeOptimizer(),
    )

    assert calls == [
        ("artifact", 2), ("optimizer", 2),
        ("artifact", 4), ("optimizer", 4),
        ("artifact", 5),
    ]
    assert summary["rounds"] == 5
    assert summary["self_improvement"]["active_version"] == "v000"
    assert [
        event["status"] for event in summary["self_improvement"]["events"]
    ] == ["no_change", "no_change"]


def test_supervisor_does_not_optimize_after_final_interval_boundary(
    tmp_path: Path,
):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    _set_max_rounds(task, 4)
    run_dir = tmp_path / "run"
    calls = []

    def artifact_runner(_config, _run_dir, *, target_rounds, **_kwargs):
        calls.append(("artifact", target_rounds))
        _write_rounds(run_dir, target_rounds)
        return {"rounds": target_rounds}

    class NoChangeOptimizer:
        def run(self, *, prompt_dir, **_kwargs):
            calls.append(("optimizer", len(
                (run_dir / "history.jsonl").read_text().splitlines()
            )))
            _write_report(Path(prompt_dir))

    supervisor.run(
        task, run_dir, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: NoChangeOptimizer(),
    )

    assert calls == [
        ("artifact", 2), ("optimizer", 2),
        ("artifact", 4),
    ]


def test_supervisor_returns_complete_summary_when_run_is_already_done(
    tmp_path: Path,
):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    run_dir = tmp_path / "run"
    _write_rounds(run_dir, 5)
    expected = _summary(run_dir) | {"rounds": 5}
    calls = []

    def artifact_runner(
        _config, _run_dir, *, target_rounds, continue_run, prompt_dir,
    ):
        assert Path(prompt_dir) == run_dir / "self_improvement" / "prompts"
        calls.append((target_rounds, continue_run))
        return expected

    class UnusedOptimizer:
        def run(self, **_kwargs):
            pytest.fail("optimizer called")

    summary = supervisor.run(
        task, run_dir, continue_run=True, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: UnusedOptimizer(),
    )

    assert calls == [(5, True)]
    assert summary == expected | {
        "self_improvement": {"active_version": "v000", "events": []},
    }


def test_supervisor_requires_explicit_continue_for_existing_rounds(
    tmp_path: Path,
):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    run_dir = tmp_path / "run"
    _write_rounds(run_dir, 1)

    with pytest.raises(ValueError, match="--continue"):
        supervisor.run(
            task, run_dir,
            artifact_runner=lambda *_args, **_kwargs: pytest.fail(
                "artifact loop called"
            ),
            optimizer_factory=lambda **_kwargs: pytest.fail("optimizer created"),
        )

    assert not (run_dir / "self_improvement").exists()


def test_supervisor_rejects_a_second_active_supervisor(tmp_path: Path):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock = (run_dir / ".self-improvement.lock").open("w")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(loop.RunLockError, match="self-improvement supervisor"):
            supervisor.run(
                task, run_dir,
                artifact_runner=lambda *_args, **_kwargs: pytest.fail(
                    "artifact loop called"
                ),
                optimizer_factory=lambda **_kwargs: pytest.fail(
                    "optimizer created"
                ),
            )
    finally:
        lock.close()


def test_supervisor_snapshots_accepted_prompt_change(tmp_path: Path):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    run_dir = tmp_path / "run"

    def artifact_runner(_config, _run_dir, *, target_rounds, **_kwargs):
        _write_rounds(run_dir, target_rounds)
        return {"rounds": target_rounds}

    class RewritingOptimizer:
        def __init__(self):
            self.calls = 0

        def run(self, *, prompt_dir, **_kwargs):
            self.calls += 1
            Path(prompt_dir, "proposer.md").write_text(
                f"rewritten proposer {self.calls}\n"
            )
            _write_report(Path(prompt_dir))

    supervisor.run(
        task, run_dir, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: RewritingOptimizer(),
    )

    root = run_dir / "self_improvement"
    history = PromptHistory(root / "prompts", root / "prompt_history")
    assert history.state["active_version"] == "v002"
    assert (history.history_dir / "v001/proposer.md").read_text() == "rewritten proposer 1\n"


def test_supervisor_restores_parent_after_optimizer_error(tmp_path: Path):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    run_dir = tmp_path / "run"

    def artifact_runner(_config, _run_dir, *, target_rounds, **_kwargs):
        _write_rounds(run_dir, target_rounds)
        return {"rounds": target_rounds}

    class BrokenOptimizer:
        def run(self, *, prompt_dir, **_kwargs):
            Path(prompt_dir, "proposer.md").write_text("partial")
            raise RuntimeError("agent crashed")

    supervisor.run(
        task, run_dir, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: BrokenOptimizer(),
    )

    root = run_dir / "self_improvement"
    history = PromptHistory(root / "prompts", root / "prompt_history")
    assert history.state["active_version"] == "v000"
    assert "You are the PROPOSER" in (history.prompt_dir / "proposer.md").read_text()
    assert [event["status"] for event in history.events()] == ["rejected", "rejected"]


def test_supervisor_removes_unexpected_files_after_rejection(tmp_path: Path):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    run_dir = tmp_path / "run"

    def artifact_runner(_config, _run_dir, *, target_rounds, **_kwargs):
        _write_rounds(run_dir, target_rounds)
        return {"rounds": target_rounds}

    class PollutingOptimizer:
        def run(self, *, prompt_dir, **_kwargs):
            Path(prompt_dir, "unexpected.txt").write_text("outside contract")
            _write_report(Path(prompt_dir))

    supervisor.run(
        task, run_dir, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: PollutingOptimizer(),
    )

    prompt_dir = run_dir / "self_improvement" / "prompts"
    assert {path.name for path in prompt_dir.iterdir()} == {
        f"{role}.md" for role in PROMPT_NAMES
    }


def test_each_run_starts_from_its_own_v000(tmp_path: Path):
    task = _task_file(tmp_path, _enabled(interval_rounds=2))
    _set_max_rounds(task, 1)

    def artifact_runner(_config, run_dir, *, target_rounds, **_kwargs):
        _write_rounds(Path(run_dir), target_rounds)
        return {"rounds": target_rounds}

    class UnusedOptimizer:
        def run(self, **_kwargs):
            pytest.fail("optimizer called")

    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    supervisor.run(
        task, run_a, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: UnusedOptimizer(),
    )
    history_a = PromptHistory(
        run_a / "self_improvement" / "prompts",
        run_a / "self_improvement" / "prompt_history",
    )
    (history_a.prompt_dir / "proposer.md").write_text("run A prompt")
    assert history_a.snapshot(1, OptimizerReport(
        diagnosis="run A only", evidence=[],
    )) == "v001"

    supervisor.run(
        task, run_b, artifact_runner=artifact_runner,
        optimizer_factory=lambda **_kwargs: UnusedOptimizer(),
    )
    history_b = PromptHistory(
        run_b / "self_improvement" / "prompts",
        run_b / "self_improvement" / "prompt_history",
    )

    assert history_a.state["active_version"] == "v001"
    assert history_b.state["active_version"] == "v000"
    assert "You are the PROPOSER" in (
        history_b.prompt_dir / "proposer.md"
    ).read_text()


def _summary(run_dir: Path) -> dict:
    return {
        "best_sha": None, "best_score": -1.0, "rounds": 0,
        "repo": str(run_dir / "repo"),
    }


def test_cli_dispatches_enabled_run_to_supervisor(monkeypatch, tmp_path: Path):
    task = _task_file(tmp_path, _enabled())
    run_dir = tmp_path / "run"
    calls = []
    monkeypatch.setattr(
        supervisor, "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or _summary(run_dir),
    )
    monkeypatch.setattr(
        loop, "run", lambda *_args, **_kwargs: pytest.fail("inner loop called directly"),
    )

    cli.main(["run", "--config", str(task), "--run-dir", str(run_dir)])

    assert len(calls) == 1


def test_cli_keeps_disabled_run_on_artifact_loop(monkeypatch, tmp_path: Path):
    task = _task_file(tmp_path)
    run_dir = tmp_path / "run"
    calls = []
    monkeypatch.setattr(
        loop, "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or _summary(run_dir),
    )
    monkeypatch.setattr(
        supervisor, "run", lambda *_args, **_kwargs: pytest.fail("supervisor called"),
    )

    cli.main(["run", "--config", str(task), "--run-dir", str(run_dir)])

    assert len(calls) == 1


def test_cli_rejects_static_proposals_with_self_improvement(tmp_path: Path):
    task = _task_file(tmp_path, _enabled())
    proposals = tmp_path / "proposals.yaml"
    proposals.write_text("- change one thing\n")
    with pytest.raises(SystemExit):
        cli.main([
            "run", "--config", str(task), "--run-dir", str(tmp_path / "run"),
            "--proposals", str(proposals),
        ])
