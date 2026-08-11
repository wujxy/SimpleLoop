#!/usr/bin/env python3
"""Run one disposable real-Claude Executor delivery through Apptainer."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from simpleloop import config as config_mod
from simpleloop.container.runtime import ApptainerRuntime, executor_mount_map
from simpleloop.harness.workspace import Workspace
from simpleloop.roles.agent import Agent
from simpleloop.roles.executor import execute


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


def _seed_repo(repo: Path, editable: list[str]) -> Path:
    repo.mkdir()
    for rel in editable:
        path = repo / rel
        if path.name == "CMakeLists.txt":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# smoke fixture\n", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    target = repo / "OMILRECV2" / "src" / "smoke.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("before\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", ".")
    _git(
        repo, "-c", "user.name=SimpleLoop", "-c",
        "user.email=loop@example.invalid", "commit", "-m", "smoke base",
    )
    return target.relative_to(repo)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = config_mod.load(args.config)

    with tempfile.TemporaryDirectory(prefix="simpleloop-executor-smoke-") as root:
        root_path = Path(root)
        source = root_path / "source"
        target = _seed_repo(source, cfg["editable_paths"])
        run_dir = root_path / "run"
        workspace = Workspace(
            run_dir=run_dir,
            repo_path=source,
            baseline_ref="HEAD",
            editable=cfg["editable_paths"],
        )
        workspace.setup()
        worktree = workspace.add_worktree("smoke", workspace.baseline_sha())
        runtime = ApptainerRuntime(
            image=cfg["runtime_image"],
            binds=cfg["runtime_binds"],
            run_dir=run_dir,
        )
        mounts = executor_mount_map(cfg)
        runtime.executor_preflight(worktree=worktree, mounts=mounts)
        print("EXECUTOR_PREFLIGHT=PASS", flush=True)

        executor_cfg = cfg["roles"]["executor"]
        agent = Agent(
            runtime=runtime,
            command="claude",
            timeout_seconds=min(cfg.get("agent_timeout_seconds", 600), 600),
            allowed_tools="Read,Edit,Write,Bash",
            max_output_tokens=cfg.get("agent_max_output_tokens", 64000),
            model=executor_cfg["model"],
            base_url=executor_cfg["base_url"],
            mounts=mounts,
        )
        try:
            result = execute(
                agent,
                proposal=(
                    "Use the Bash tool to run `pwd` and `true`. Then replace "
                    "the exact contents of OMILRECV2/src/smoke.txt with "
                    "`changed` followed by a newline. Change no other file."
                ),
                goal="Prove the isolated Executor can use Bash and deliver.",
                workspace=workspace,
                worktree=worktree,
                round_id="smoke",
            )
            if result.sha is None:
                raise RuntimeError(result.reason or "Executor delivered no commit")
            if result.changed_paths != [str(target)]:
                raise RuntimeError(
                    f"unexpected changed paths: {result.changed_paths}",
                )
            if (worktree / target).read_text(encoding="utf-8") != "changed\n":
                raise RuntimeError("Executor did not write the expected content")
            if _git(source, "status", "--porcelain"):
                raise RuntimeError("smoke modified its source repository")
            print(f"EXECUTOR_SMOKE=PASS sha={result.sha}", flush=True)
        finally:
            workspace.remove_worktree("smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
