"""Executor mount-world construction: ``exec_argv(mounts=...)`` builds a
container that exposes ONLY the declared rw/ro subpaths under /work and leaves
everything else absent. No project names are hardcoded in the runtime — the
mount map is pure data filled from config."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop.container.runtime import ApptainerRuntime, MountMap


def _runtime(tmp_path: Path, binds=()) -> ApptainerRuntime:
    image = tmp_path / "image.sif"
    image.write_text("dummy")
    return ApptainerRuntime(image=image, binds=binds, run_dir=tmp_path)


def test_exec_argv_with_mounts_builds_subset_world(tmp_path: Path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "a.cc").write_text("x")
    (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "t.cc").write_text("secret")
    # 'build' does NOT exist yet — the runtime must pre-create it (bind source
    # must exist).
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bind_dep = tmp_path / "dep"
    bind_dep.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    rt = _runtime(tmp_path, binds=[bind_dep])

    argv = rt.exec_argv(
        ["claude", "-p"], cwd=wt,
        mounts=MountMap(
            rw=("src", "build"), ro=("tests",),
            external_ro=(str(external),),
        ),
        scaffold=scaffold, home=home,
    )

    # container is contained + host worktree/cwd/home/hostfs do NOT leak in
    assert "--containall" in argv
    assert argv[argv.index("--no-mount") + 1] == "cwd,home,hostfs"
    assert f"{home}:{rt.executor_home}:rw" in argv
    # scaffold -> /work (the container root of the constructed world)
    assert f"{scaffold}:/work:rw" in argv
    # declared rw subpaths mounted rw, ro subpaths mounted ro — under /work
    assert f"{wt / 'src'}:/work/src:rw" in argv
    assert f"{wt / 'build'}:/work/build:rw" in argv
    assert f"{wt / 'tests'}:/work/tests:ro" in argv
    # evaluator binds do not leak; explicitly declared Executor deps are ro.
    assert f"{bind_dep}:{bind_dep}" not in argv
    assert f"{external}:{external}:ro" in argv
    # cwd is the constructed /work, not the host worktree
    assert argv[argv.index("--cwd") + 1] == "/work"
    # the run_dir wholesale mount is NOT present (no leak)
    assert f"{tmp_path}:{tmp_path}:rw" not in argv
    # the missing build dir was pre-created so the bind source exists
    assert (wt / "build").is_dir()


def test_exec_argv_without_mounts_is_legacy_whole_run_dir(tmp_path: Path):
    wt = tmp_path / "wt"
    wt.mkdir()
    rt = _runtime(tmp_path)
    argv = rt.exec_argv(["claude", "-p"], cwd=wt)
    # legacy path: run_dir mounted rw, cwd is the host worktree, no containall
    assert f"{tmp_path}:{tmp_path}:rw" in argv
    assert "--containall" not in argv
    assert argv[argv.index("--cwd") + 1] == str(wt.resolve())


def test_read_only_mount_missing_is_skipped_not_fatal(tmp_path: Path, capsys):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    rt = _runtime(tmp_path)
    # 'stale' ro path does not exist in the worktree → warned + skipped, not raised
    argv = rt.exec_argv(
        ["claude", "-p"], cwd=wt,
        mounts=MountMap(rw=("src",), ro=("stale",)),
        scaffold=scaffold, home=home,
    )
    assert "/work/stale" not in argv
    assert "/work/src:rw" in " ".join(argv)
    out = capsys.readouterr().out
    assert "read-only mount 'stale'" in out


@pytest.mark.parametrize("missing", ["scaffold", "home"])
def test_mount_world_requires_private_directories(tmp_path: Path, missing: str):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    scaffold = None if missing == "scaffold" else tmp_path / "scaffold"
    home = None if missing == "home" else tmp_path / "home"
    if scaffold is not None:
        scaffold.mkdir()
    if home is not None:
        home.mkdir()

    with pytest.raises(ValueError, match=missing):
        _runtime(tmp_path).exec_argv(
            ["true"], cwd=wt, mounts=MountMap(rw=("src",)),
            scaffold=scaffold, home=home,
        )


def test_private_home_override_is_explicit(tmp_path: Path):
    rt = _runtime(tmp_path)
    env = rt.subprocess_env({"HOME": str(rt.executor_home)})
    assert env["APPTAINERENV_HOME"] == str(rt.executor_home)


def test_executor_preflight_uses_production_mount_world(
    tmp_path: Path, monkeypatch,
):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    hidden = wt / "protected"
    hidden.mkdir()
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    rt = _runtime(tmp_path, binds=[evaluator])
    captured = {}

    def fake_run(argv, **kwargs):
        assert (wt / ".simpleloop-preflight-hidden").is_file()
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="preflight: PASS\n", stderr="")

    monkeypatch.setattr("simpleloop.container.runtime.subprocess.run", fake_run)

    rt.executor_preflight(
        worktree=wt,
        mounts=MountMap(rw=("src",), external_ro=(str(external),)),
    )

    joined = " ".join(captured["argv"])
    assert "--no-mount cwd,home,hostfs" in joined
    assert f"{external}:{external}:ro" in joined
    assert str(evaluator) not in joined
    assert captured["env"]["APPTAINERENV_HOME"] == str(rt.executor_home)
    assert not (wt / ".simpleloop-preflight-hidden").exists()


def test_executor_preflight_reports_capability_failure(tmp_path: Path, monkeypatch):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    rt = _runtime(tmp_path)
    monkeypatch.setattr(
        "simpleloop.container.runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=126, stdout="", stderr="executor home is not writable",
        ),
    )

    with pytest.raises(Exception, match="executor home is not writable"):
        rt.executor_preflight(worktree=wt, mounts=MountMap(rw=("src",)))


@pytest.mark.parametrize("raw,expected", [
    ("src/**", "src"),
    ("src/**/", "src"),
    ("src", "src"),
    ("a/b", "a/b"),
    ("CMakeLists.txt", "CMakeLists.txt"),
])
def test_normalize_mount_path_strips_legacy_glob(raw, expected):
    from simpleloop.config import _normalize_mount_path
    assert _normalize_mount_path(raw) == expected
