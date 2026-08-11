"""MVP operational layer: config snapshot provenance, configurable limits,
run_dir lock, summary.json, and `simpleloop export`."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import loop as loop_mod
from simpleloop.harness import export as export_mod
from simpleloop.harness.store import Store
from simpleloop.harness.workspace import Workspace

SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


# ---- config: new optional limit keys ---------------------------------------

def _write_task(tmp_path: Path, loop_extra: dict | None = None,
                eval_extra: dict | None = None) -> Path:
    repo = tmp_path / "srcrepo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    image = tmp_path / "img.sif"
    image.write_bytes(b"sif")
    raw = {
        "kind": "task",
        "task": {"goal": "test"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 1, **(loop_extra or {})},
        "runtime": {"image": str(image)},
        "source": {"path": str(repo)},
        "eval": {
            "commands": ["run-eval"],
            "metrics": SCHEMA,
            **(eval_extra or {}),
        },
    }
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_limit_keys_default(tmp_path: Path):
    cfg = config_mod.load(_write_task(tmp_path))
    assert cfg["eval_timeout_seconds"] == 600
    assert cfg["eval_output_cap_chars"] == 16000
    assert cfg["eval_history_cap_chars"] == 6000
    assert cfg["agent_max_output_tokens"] == 64000


def test_limit_keys_override(tmp_path: Path):
    cfg = config_mod.load(_write_task(
        tmp_path,
        loop_extra={"agent_max_output_tokens": 32000},
        eval_extra={"timeout_seconds": 7200, "output_cap_chars": 2000,
                    "history_cap_chars": 1000},
    ))
    assert cfg["eval_timeout_seconds"] == 7200
    assert cfg["eval_output_cap_chars"] == 2000
    assert cfg["eval_history_cap_chars"] == 1000
    assert cfg["agent_max_output_tokens"] == 32000


@pytest.mark.parametrize("loop_extra,eval_extra,match", [
    (None, {"timeout_seconds": 0}, "eval.timeout_seconds"),
    (None, {"timeout_seconds": "600"}, "eval.timeout_seconds"),
    (None, {"output_cap_chars": 999}, "eval.output_cap_chars"),
    (None, {"history_cap_chars": 499}, "eval.history_cap_chars"),
    ({"agent_max_output_tokens": 7999}, None, "loop.agent_max_output_tokens"),
])
def test_limit_keys_reject_bad_values(tmp_path: Path, loop_extra, eval_extra, match):
    path = _write_task(tmp_path, loop_extra=loop_extra, eval_extra=eval_extra)
    with pytest.raises(config_mod.ConfigError, match=match):
        config_mod.load(path)


# ---- provenance: resolved snapshot + original copy -------------------------

def test_load_resolved_requires_snapshot(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="predates config snapshots"):
        config_mod.load_resolved(tmp_path)


def test_write_config_snapshot_roundtrip_and_orig_once(tmp_path: Path):
    config_path = _write_task(tmp_path)
    cfg = config_mod.load(config_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    loop_mod._write_config_snapshot(cfg, config_path, run_dir)
    assert config_mod.load_resolved(run_dir) == cfg
    orig = run_dir / "config.orig.yaml"
    assert orig.read_text() == config_path.read_text()

    # A later session (e.g. --continue with a bumped max_rounds) overwrites the
    # resolved snapshot but never the original copy.
    bumped = dict(cfg, max_rounds=9)
    config_path.write_text(config_path.read_text() + "# edited\n")
    loop_mod._write_config_snapshot(bumped, config_path, run_dir)
    assert config_mod.load_resolved(run_dir)["max_rounds"] == 9
    assert "# edited" not in orig.read_text()


def test_snapshot_serializes_path_values(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    loop_mod._write_config_snapshot(
        {"repo_path": tmp_path / "source", "metrics": SCHEMA},
        tmp_path / "no-such-config.yaml", run_dir)
    resolved = config_mod.load_resolved(run_dir)
    assert resolved["repo_path"] == str(tmp_path / "source")
    assert not (run_dir / "config.orig.yaml").exists()


# ---- run_dir lock -----------------------------------------------------------

def test_run_lock_excludes_second_holder(tmp_path: Path):
    fd = loop_mod._acquire_run_lock(tmp_path)
    assert fd is not None
    holder = json.loads((tmp_path / ".lock").read_text())
    assert holder["pid"] > 0 and holder["host"]
    with pytest.raises(loop_mod.RunLockError, match="locked by another"):
        loop_mod._acquire_run_lock(tmp_path)
    loop_mod._release_run_lock(fd)
    fd2 = loop_mod._acquire_run_lock(tmp_path)
    assert fd2 is not None
    loop_mod._release_run_lock(fd2)


# ---- summary.json + export --------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return completed.stdout.strip()


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "file.txt").write_text("hello\n")
    _git(src, "add", "file.txt")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@e.invalid",
         "commit", "-qm", "baseline")
    return src


def test_rename_reports_both_source_and_destination_paths(tmp_path: Path):
    src = _make_source(tmp_path)
    (src / "frozen.txt").write_text("protected\n")
    _git(src, "add", "frozen.txt")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@e.invalid",
         "commit", "-qm", "add frozen file")
    run_dir = tmp_path / "run"
    ws = Workspace(run_dir, str(src), "HEAD", ["frozen.txt"])
    ws.setup()
    wt = ws.add_worktree("rename", ws.baseline_sha())
    (wt / "editable").mkdir()
    _git(wt, "mv", "frozen.txt", "editable/moved.txt")

    changed = ws.changed_paths(wt)

    assert changed == ["editable/moved.txt", "frozen.txt"]


def _seed_run(tmp_path: Path):
    """A real one-round run_dir: cloned repo, one committed candidate, history,
    and a resolved-config snapshot."""
    src = _make_source(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ws = Workspace(run_dir=run_dir, repo_path=str(src), baseline_ref="HEAD",
                   copy_entries=["*"])
    ws.setup()
    baseline = ws.baseline_sha()
    wt = ws.add_worktree("0-c0", baseline)
    (wt / "file.txt").write_text("hello world\n")
    sha = ws.commit(wt, "0-c0", ["file.txt"])
    ws.remove_worktree("0-c0")
    store = Store(run_dir, metrics_schema=SCHEMA)
    store.append_generation(
        0, parent_sha=baseline, selected_candidate=0, selected_sha=sha,
        candidates=[{
            "candidate": 0, "proposal": "speed it up", "sha": sha,
            "status": "COMPLETED",
            "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": True},
            "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
            "gate_passed": True, "eligible": True, "selected": True,
        }])
    cfg = {"metrics": SCHEMA, "baseline_ref": "HEAD", "repo_path": str(src)}
    loop_mod._write_config_snapshot(cfg, tmp_path / "absent.yaml", run_dir)
    return src, run_dir, ws, store, baseline, sha


def test_summary_writes_summary_json(tmp_path: Path):
    _, run_dir, ws, store, baseline, sha = _seed_run(tmp_path)
    ctx = loop_mod.RunContext(
        cfg={"metrics": SCHEMA}, run_dir=run_dir, workspace=ws, store=store,
        baseline_metrics={"SPEED_MS": 100.0, "CORRECTNESS": True})
    summary = loop_mod._summary(ctx, run_dir)
    assert summary["best_sha"] == sha
    assert summary["best_objective"] == 90.0
    assert summary["baseline_objective"] == 100.0
    assert summary["baseline_sha"] == baseline
    assert summary["final_chain_sha"] == sha
    assert summary["objective_key"] == "SPEED_MS"
    assert json.loads((run_dir / "summary.json").read_text()) == summary


def test_summary_rebuilds_best_fields_from_history_on_resume(tmp_path: Path):
    _, run_dir, ws, _, _, sha = _seed_run(tmp_path)
    resumed_store = Store(run_dir, metrics_schema=SCHEMA)
    ctx = loop_mod.RunContext(
        cfg={"metrics": SCHEMA}, run_dir=run_dir, workspace=ws,
        store=resumed_store,
    )

    summary = loop_mod._summary(ctx, run_dir)

    assert summary["best_sha"] == sha
    assert summary["best_round"] == 0
    assert summary["best_candidate"] == 0


def test_export_best_produces_diff_bundle_and_notes(tmp_path: Path):
    src, run_dir, _, _, baseline, sha = _seed_run(tmp_path)
    info = export_mod.export_run(run_dir)
    assert info["sha"] == sha
    assert info["baseline_sha"] == baseline
    assert info["objective"] == 90.0
    diff = Path(info["diff"]).read_text()
    assert "hello world" in diff
    # The squashed diff applies cleanly on the source repo's baseline tree.
    subprocess.run(["git", "-C", str(src), "apply", "--check", info["diff"]],
                   check=True)
    # The bundle verifies against the source repo (baseline prerequisite met).
    subprocess.run(["git", "-C", str(src), "bundle", "verify", info["bundle"]],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    notes = Path(info["readme"]).read_text()
    assert sha in notes and str(src) in notes


def test_export_to_branch_pushes_once(tmp_path: Path):
    src, run_dir, *_ , sha = _seed_run(tmp_path)
    info = export_mod.export_run(run_dir, to_branch="sl-best")
    assert info["branch"] == "sl-best"
    assert _git(src, "rev-parse", "refs/heads/sl-best") == sha
    with pytest.raises(export_mod.ExportError, match="already exists"):
        export_mod.export_run(run_dir, to_branch="sl-best")


def test_export_head_and_best_reject_empty_runs(tmp_path: Path):
    src = _make_source(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ws = Workspace(run_dir=run_dir, repo_path=str(src), baseline_ref="HEAD",
                   copy_entries=["*"])
    ws.setup()
    baseline = ws.baseline_sha()
    store = Store(run_dir, metrics_schema=SCHEMA)
    # one round, no commit: chain never advances, nothing eligible
    store.append_generation(
        0, parent_sha=baseline, selected_candidate=None, selected_sha=None,
        candidates=[{"candidate": 0, "proposal": "p", "sha": None,
                     "status": "NO_CHANGE", "metrics": {},
                     "gate_passed": False, "eligible": False,
                     "selected": False}])
    loop_mod._write_config_snapshot(
        {"metrics": SCHEMA, "baseline_ref": "HEAD", "repo_path": str(src)},
        tmp_path / "absent.yaml", run_dir)
    with pytest.raises(export_mod.ExportError, match="no eligible best"):
        export_mod.export_run(run_dir, what="best")
    with pytest.raises(export_mod.ExportError, match="never advanced"):
        export_mod.export_run(run_dir, what="head")
    with pytest.raises(export_mod.ExportError, match="best|head"):
        export_mod.export_run(run_dir, what="tip")


def test_cli_plot_falls_back_to_resolved_snapshot(tmp_path: Path):
    from simpleloop import cli as cli_mod
    _, run_dir, *_ = _seed_run(tmp_path)
    cli_mod.main(["plot", "--run-dir", str(run_dir)])
    assert (run_dir / "progress.png").exists()
