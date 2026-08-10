from __future__ import annotations

from pathlib import Path

import pytest

from simpleloop import cli as cli_mod
from scripts import proposer_harness as harness


def _summary(tmp_path: Path) -> harness.ProposerHarnessSummary:
    return harness.ProposerHarnessSummary(
        result_path=tmp_path / "result.json",
        report_path=tmp_path / "proposals.md",
        proposal_count=1,
        abstained=False,
        base_sha="base",
    )


def test_cli_propose_forwards_arguments(monkeypatch, tmp_path, capsys):
    captured = {}

    def fake_run(config, output, **kwargs):
        captured.update(
            config=config,
            output=output,
            from_run=kwargs["from_run"],
            seed=kwargs["seed"],
        )
        return _summary(tmp_path)

    monkeypatch.setattr(harness, "run_proposer", fake_run)

    harness.main([
        "--config", "task.yaml",
        "--output-dir", "trial",
        "--from-run", "old-run",
        "--seed", "42",
    ])

    assert captured == {
        "config": "task.yaml",
        "output": "trial",
        "from_run": "old-run",
        "seed": 42,
    }
    output = capsys.readouterr().out
    assert "1 proposal(s)" in output
    assert "result.json" in output
    assert "proposals.md" in output


def test_cli_propose_reports_harness_error(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise harness.ProposerHarnessError("bad history")

    monkeypatch.setattr(harness, "run_proposer", fail)

    with pytest.raises(SystemExit) as raised:
        harness.main([
            "--config", "task.yaml",
            "--output-dir", "trial",
        ])

    assert raised.value.code == 1
    assert "Proposer error: bad history" in capsys.readouterr().err


def test_simpleloop_cli_does_not_register_propose(capsys):
    with pytest.raises(SystemExit) as raised:
        cli_mod.main(["propose"])

    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
