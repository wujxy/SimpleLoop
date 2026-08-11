from __future__ import annotations

from pathlib import Path
import subprocess

from simpleloop.harness.workspace import Workspace


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "seed"
    (repo / "src").mkdir(parents=True)
    (repo / "reference").mkdir()
    (repo / "src" / "main.cc").write_text("int main() {}\n", encoding="utf-8")
    (repo / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")
    (repo / "reference" / "golden.txt").write_text("immutable\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-m", "seed")
    return repo


def test_workspace_materializes_only_requested_seed_entries(tmp_path: Path):
    seed = _seed_repo(tmp_path)
    workspace = Workspace(
        tmp_path / "run", str(seed), "HEAD", ["src", "CMakeLists.txt"])
    workspace.setup()

    assert (workspace.repo / "src" / "main.cc").is_file()
    assert (workspace.repo / "CMakeLists.txt").is_file()
    assert not (workspace.repo / "reference").exists()
    assert (workspace.repo / ".git").exists()


def test_worktree_can_delete_and_create_any_package_file(tmp_path: Path):
    seed = _seed_repo(tmp_path)
    workspace = Workspace(
        tmp_path / "run", str(seed), "HEAD", ["src", "CMakeLists.txt"])
    workspace.setup()
    worktree = workspace.add_worktree("candidate", workspace.baseline_sha())

    (worktree / "src" / "main.cc").unlink()
    (worktree / "new_layout").mkdir()
    (worktree / "new_layout" / "kernel.cc").write_text(
        "int f() { return 1; }\n", encoding="utf-8")

    assert set(workspace.changed_paths(worktree)) == {
        "src/main.cc", "new_layout/kernel.cc",
    }
