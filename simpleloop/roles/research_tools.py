"""Read-only factual lookup for the Proposer's research phase."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from ..harness.memory import resolve_episode


@dataclass(frozen=True)
class ResearchToolSpec:
    """Prompt description owned by the research tool boundary."""

    action: str
    schema: str
    description: str


RESEARCH_TOOL_SPECS = (
    ResearchToolSpec(
        action="run_research_command",
        schema=(
            '{"action":"run_research_command","command":"...",'
            '"cwd":"source|scratch"}'
        ),
        description=(
            "Inspect the accepted source, Git state, or run evidence with a "
            "bounded shell command. Source is read-only; scratch is writable."
        ),
    ),
    ResearchToolSpec(
        action="inspect_episode",
        schema='{"action":"inspect_episode","ref":"r<round>c<candidate>"}',
        description=(
            "Resolve one candidate episode by ref (e.g. r0c1). Returns "
            "proposal, status, gates, metrics, eval output, parent_sha, and "
            "candidate_sha. Use run_research_command with "
            "`git diff parent_sha..candidate_sha` to see code changes."
        ),
    ),
)


def render_research_tool_prompt() -> str:
    return "\n".join(
        f"- {spec.schema}\n  {spec.description}"
        for spec in RESEARCH_TOOL_SPECS
    )


def render_history_directory(history: list[dict]) -> str:
    """Compact experiment directory rendered from history for the proposer
    context — not a stored artifact.

    Every prior candidate is listed as ``ref: note``. Notes are the previous
    round's proposer's frozen summary; the most recent round's notes are blank
    because that round's proposer writes them. Treat notes as navigation, not
    fact — verify with inspect_episode.
    """
    if not history:
        return "(no prior experiments)"
    rounds = sorted(
        (record for record in history if isinstance(record, dict)),
        key=lambda r: r.get("round", 0),
    )
    lines: list[str] = []
    for record in rounds:
        round_id = record.get("round", 0)
        for candidate in record.get("candidates") or []:
            cid = candidate.get("candidate", 0)
            ref = f"r{round_id}c{cid}"
            note = (candidate.get("note") or "").strip()
            lines.append(f"  {ref}: {note}" if note else f"  {ref}:")
    return "\n".join(lines) or "(no prior experiments)"


class ResearchCommandRunner:
    """Run one bounded Bash command inside the research container."""

    def __init__(
        self,
        *,
        runtime,
        source: Path,
        repo: Path,
        history_dir: Path,
        scratch: Path,
        timeout_seconds: int,
        output_cap_chars: int,
    ):
        self.runtime = runtime
        self.source = Path(source)
        self.repo = Path(repo)
        self.history_dir = Path(history_dir)
        self.scratch = Path(scratch)
        self.timeout_seconds = timeout_seconds
        self.output_cap_chars = output_cap_chars

    def run(
        self,
        command: str,
        *,
        cwd: str = "source",
        timeout_seconds: float | None = None,
    ) -> dict:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("research command must be non-empty")
        if cwd not in {"source", "scratch"}:
            raise ValueError("research cwd must be 'source' or 'scratch'")
        git_dir = self._worktree_git_dir()
        payload = [
            "env",
            f"GIT_DIR={git_dir}",
            "GIT_COMMON_DIR=/repo/.git",
            "GIT_WORK_TREE=/source",
            "bash",
            "-lc",
            command,
        ]
        argv = self.runtime.research_exec_argv(
            payload,
            source=self.source,
            repo=self.repo,
            history=self.history_dir,
            scratch=self.scratch,
            cwd=cwd,
        )
        process = subprocess.Popen(
            argv,
            cwd=str(self.runtime.run_dir),
            env=self.runtime.research_subprocess_env(),
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else min(self.timeout_seconds, timeout_seconds)
        )
        stdout_result: dict = {}
        stderr_result: dict = {}
        readers = [
            Thread(
                target=_drain_bounded,
                args=(process.stdout, self.output_cap_chars, stdout_result),
            ),
            Thread(
                target=_drain_bounded,
                args=(process.stderr, self.output_cap_chars, stderr_result),
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process.pid)
            process.wait()
            returncode = None
        finally:
            if not timed_out:
                _kill_process_group(process.pid)
            for reader in readers:
                reader.join()
        stdout = stdout_result.get("text", "")
        stderr = stderr_result.get("text", "")
        output = stdout
        if stderr:
            output += "\n[stderr]\n" + stderr
        truncated = (
            stdout_result.get("truncated", False)
            or stderr_result.get("truncated", False)
            or len(output) > self.output_cap_chars
        )
        output = output[:self.output_cap_chars]
        return {
            "ok": not timed_out and returncode == 0,
            "returncode": returncode,
            "timed_out": timed_out,
            "truncated": truncated,
            "output": output,
        }

    def _worktree_git_dir(self) -> str:
        git_file = self.source / ".git"
        try:
            line = git_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"research source has no worktree metadata: {git_file}"
            ) from exc
        if not line.startswith("gitdir: "):
            raise ValueError(f"invalid worktree metadata: {git_file}")
        git_dir = Path(line.removeprefix("gitdir: "))
        if not git_dir.is_absolute():
            git_dir = git_file.parent / git_dir
        admin_root = (self.repo / ".git" / "worktrees").resolve()
        try:
            relative = git_dir.resolve().relative_to(admin_root)
        except ValueError as exc:
            raise ValueError(
                "research worktree metadata points outside the run repository"
            ) from exc
        if len(relative.parts) != 1:
            raise ValueError("invalid research worktree admin path")
        return f"/repo/.git/worktrees/{relative.as_posix()}"


def _drain_bounded(stream, cap: int, result: dict) -> None:
    chunks: list[str] = []
    kept = 0
    truncated = False
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        room = cap - kept
        if room > 0:
            retained = chunk[:room]
            chunks.append(retained)
            kept += len(retained)
        if len(chunk) > max(room, 0):
            truncated = True
    result.update(text="".join(chunks), truncated=truncated)


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class ResearchTools:
    """Dispatch the Proposer's non-terminal research actions."""

    def __init__(
        self,
        *,
        runtime,
        source: Path,
        repo: Path,
        history_dir: Path,
        scratch: Path,
        history: list[dict],
        command_timeout_seconds: int,
        command_output_cap_chars: int,
    ):
        self.history = history
        self.command_timeout_seconds = command_timeout_seconds
        self.command_runner = ResearchCommandRunner(
            runtime=runtime,
            source=source,
            repo=repo,
            history_dir=history_dir,
            scratch=scratch,
            timeout_seconds=command_timeout_seconds,
            output_cap_chars=command_output_cap_chars,
        )

    def execute(self, action: dict, *, deadline: float) -> dict:
        name = action["action"]
        try:
            if name == "run_research_command":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"ok": False, "error": "proposer deadline exceeded"}
                return self.command_runner.run(
                    action["command"],
                    cwd=action["cwd"],
                    timeout_seconds=min(
                        self.command_timeout_seconds, remaining,
                    ),
                )
            if name == "inspect_episode":
                return {
                    "ok": True,
                    "result": resolve_episode(self.history, action["ref"]),
                }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        raise ValueError(f"unsupported research action: {name}")
