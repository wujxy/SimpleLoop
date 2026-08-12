"""Executor mount-world construction: ``exec_argv(mounts=...)`` mounts the
whole candidate worktree read-only at /work and overlays each declared editable
path read-write on top. Everything is visible + runnable; writes outside the
editable set hit EROFS. No project names are hardcoded in the runtime — the
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


def test_exec_argv_with_mounts_overlays_editable_on_read_only_worktree(tmp_path: Path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "a.cc").write_text("x")
    (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "t.cc").write_text("read me")
    (wt / "CMakeLists.txt").write_text("project(x)")
    # 'build' does NOT exist yet — the runtime must pre-create it (the rw
    # overlay bind source must exist, and the eval writes build outputs there).
    home = tmp_path / "home"
    home.mkdir()
    bind_dep = tmp_path / "dep"        # a runtime.binds entry (evaluator dep)
    bind_dep.mkdir()
    external = tmp_path / "external"   # an external_ro (e.g. /cvmfs data)
    external.mkdir()
    rt = _runtime(tmp_path, binds=[bind_dep])

    argv = rt.exec_argv(
        ["claude", "-p"], cwd=wt,
        mounts=MountMap(
            rw=("src", "build"),
            external_ro=(str(external),),
        ),
        home=home,
    )

    # container is contained + host worktree/cwd/home/hostfs do NOT leak in
    assert "--containall" in argv
    assert argv[argv.index("--no-mount") + 1] == "cwd,home,hostfs"
    assert f"{home}:{rt.executor_home}:rw" in argv
    # the WHOLE worktree is the read-only /work base (everything visible)
    assert f"{wt}:/work:ro" in argv
    # declared editable subpaths overlaid :rw on top of the ro base
    assert f"{wt / 'src'}:/work/src:rw" in argv
    assert f"{wt / 'build'}:/work/build:rw" in argv
    # external_ro mounted :ro as-is; runtime.binds (evaluator deps) do NOT leak
    assert f"{external}:{external}:ro" in argv
    assert f"{bind_dep}:{bind_dep}" not in argv
    # cwd is the constructed /work, not the host worktree
    assert argv[argv.index("--cwd") + 1] == "/work"
    # the run_dir wholesale mount is NOT present (no leak)
    assert f"{tmp_path}:{tmp_path}:rw" not in argv
    # no empty-scaffold /work mount anymore — the worktree itself is /work
    assert not any(a.endswith(":/work:rw") for a in argv)
    # the missing build dir was pre-created so the overlay bind source exists
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


def test_mount_world_creates_missing_editable_dir(tmp_path: Path):
    """A writable editable path that doesn't exist yet (e.g. a build-output dir
    in a fresh worktree) is created so the :rw overlay bind source exists."""
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    rt = _runtime(tmp_path)
    rt.exec_argv(
        ["claude", "-p"], cwd=wt,
        mounts=MountMap(rw=("src", "build", "TEMP")),
        home=home,
    )
    assert (wt / "build").is_dir()
    assert (wt / "TEMP").is_dir()


def test_mount_world_requires_home(tmp_path: Path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    with pytest.raises(ValueError, match="home"):
        _runtime(tmp_path).exec_argv(
            ["true"], cwd=wt, mounts=MountMap(rw=("src",)), home=None,
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
    (wt / "protected").mkdir()           # a non-editable path (ro via the base)
    evaluator = tmp_path / "evaluator"   # a runtime.binds entry
    evaluator.mkdir()
    external = tmp_path / "external"     # an external_ro
    external.mkdir()
    rt = _runtime(tmp_path, binds=[evaluator])
    captured = {}

    def fake_run(argv, **kwargs):
        # the harness writes the sentinel at the worktree root before the run
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
    # whole worktree is the ro /work base; editable overlaid rw; external ro
    assert f"{wt}:/work:ro" in joined
    assert f"{wt / 'src'}:/work/src:rw" in joined
    assert f"{external}:{external}:ro" in joined
    # runtime.binds (evaluator) and the empty scaffold do NOT appear
    assert str(evaluator) not in joined
    assert "/work:rw" not in joined
    assert captured["env"]["APPTAINERENV_HOME"] == str(rt.executor_home)
    # sentinel cleaned up after the run
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
    # a trailing /** (legacy editable-glob form) is still stripped to its dir
    ("src/**", "src"),
    ("src/**/", "src"),
    # real dir/file paths are passed through unchanged
    ("src", "src"),
    ("a/b", "a/b"),
    ("CMakeLists.txt", "CMakeLists.txt"),
    ("build", "build"),
])
def test_normalize_mount_path_strips_legacy_trailing_glob(raw, expected):
    from simpleloop.config import _normalize_mount_path
    assert _normalize_mount_path(raw) == expected


@pytest.mark.parametrize("glob_path", [
    "src/**/*.cc",     # mid-path glob — used to create a literal '**' dir
    "src/**/*.h",
    "src/*.cc",
    "src/cache[a-z]",
])
def test_editable_paths_rejects_glob_patterns(tmp_path: Path, glob_path: str):
    """Glob characters in editable_paths are rejected at config load — the
    writable world must be real dirs/files a bind can target."""
    from simpleloop import config as config_mod
    raw = _base_task(tmp_path)
    raw["safety"]["editable_paths"] = [glob_path]
    path = tmp_path / "task.yaml"
    path.write_text(__import__("yaml").safe_dump(raw), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="glob pattern not supported"):
        config_mod.load(path)


def _base_task(tmp_path: Path) -> dict:
    """Minimal valid task dict for config-load tests."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    image = tmp_path / "image.sif"
    image.write_text("dummy")
    return {
        "kind": "task",
        "task": {"goal": "faster"},
        "safety": {"editable_paths": ["src"]},
        "loop": {"max_rounds": 1},
        "source": {"path": str(repo)},
        "runtime": {"image": str(image)},
        "eval": {"commands": ["true"], "metrics": {
            "objective": {"key": "SPEED_MS", "lower_is_better": True}}},
    }
