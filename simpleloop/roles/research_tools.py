"""Factual lookup for the Proposer's research phase.

Two families of tools:
  - ``ResearchCommandRunner`` runs a bounded shell command in a sandboxed
    Apptainer boundary (workspace read-write, repo read-only for git history,
    scratch writable, no network).
  - ``ScientificMemoryTools`` dispatches the Proposer's memory operations to
    ``MemoryService``.

``ResearchTools`` is the façade the Proposer's runtime speaks to.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from ..processes import CHILD_PROCESSES


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
            '"cwd":"workspace|scratch"}'
        ),
        description=(
            "Run a bounded shell command in your writable lab (/workspace) or "
            "scratch (/scratch). /workspace is the accepted source tree "
            "materialized read-write: read it, write scratch code, compile, "
            "run toys to understand the code. Git history (any prior "
            "experiment SHA) is readable via /repo; you cannot commit."
        ),
    ),
    ResearchToolSpec(
        action="inspect_episode",
        schema='{"action":"inspect_episode","ref":"r<round>c<candidate>"}',
        description=(
            "Resolve one candidate experiment by ref (e.g. r0c1). Returns "
            "proposal, status, gates, metrics, eval output, parent/candidate "
            "shas, and its finding_id. Pair with run_research_command + "
            "'git diff parent_sha..candidate_sha' to see the code change."
        ),
    ),
    ResearchToolSpec(
        action="list_findings",
        schema=(
            '{"action":"list_findings","state":"active|open|dormant|'
            'archived|all","limit":1-20}'
        ),
        description=(
            "List Findings (research question containers) by operational "
            "state. Returns id, question, mechanisms, code_regions, "
            "experiment_refs, and stats."
        ),
    ),
    ResearchToolSpec(
        action="search_findings",
        schema='{"action":"search_findings","query":"...","limit":1-20}',
        description=(
            "Rank existing Findings by BM25+MMR against the query. Use to "
            "check whether your candidate research question is already open."
        ),
    ),
    ResearchToolSpec(
        action="inspect_finding",
        schema='{"action":"inspect_finding","finding_id":"F-NNN"}',
        description=(
            "Return the full record for one Finding: question, scope, "
            "operational state, experiment_refs, and derived stats. Never "
            "contains an LLM-authored conclusion."
        ),
    ),
    ResearchToolSpec(
        action="search_experiments",
        schema=(
            '{"action":"search_experiments","query":"...",'
            '"filters":{"gate_passed":bool,"selected":bool,'
            '"finding_id":"F-NNN","changed_path":"path/prefix",'
            '"round_min":int,"round_max":int,"status":"..."},'
            '"limit":1-50,"buckets":true|false}'
        ),
        description=(
            "Retrieve prior experiments. Default buckets=true returns "
            "{relevant, contrasting, diverse}; buckets=false returns a flat "
            "top-K list. Filters stack as AND. Each hit includes finding_id "
            "(if any) so you can chain into inspect_finding."
        ),
    ),
)


MEMORY_TOOL_ACTIONS = frozenset({
    "inspect_episode",
    "list_findings",
    "search_findings",
    "inspect_finding",
    "search_experiments",
})


def render_research_tool_prompt() -> str:
    return "\n".join(
        f"- {spec.schema}\n  {spec.description}"
        for spec in RESEARCH_TOOL_SPECS
    )


class ResearchCommandRunner:
    """Run one bounded Bash command inside the research container."""

    def __init__(
        self,
        *,
        runtime,
        workspace: Path,
        repo: Path,
        history_dir: Path | None,
        scratch: Path,
        timeout_seconds: int,
        output_cap_chars: int,
    ):
        self.runtime = runtime
        self.workspace = Path(workspace)
        self.repo = Path(repo)
        self.history_dir = Path(history_dir) if history_dir is not None else None
        self.scratch = Path(scratch)
        self.timeout_seconds = timeout_seconds
        self.output_cap_chars = output_cap_chars

    def run(
        self,
        command: str,
        *,
        cwd: str = "workspace",
        timeout_seconds: float | None = None,
    ) -> dict:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("research command must be non-empty")
        if cwd not in {"workspace", "scratch"}:
            raise ValueError("research cwd must be 'workspace' or 'scratch'")
        git_dir = self._worktree_git_dir()
        payload = [
            "env",
            f"GIT_DIR={git_dir}",
            "GIT_COMMON_DIR=/repo/.git",
            "GIT_WORK_TREE=/workspace",
            "bash",
            "-lc",
            command,
        ]
        argv = self.runtime.research_exec_argv(
            payload,
            workspace=self.workspace,
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
        CHILD_PROCESSES.register(process.pid)
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
            CHILD_PROCESSES.unregister(process.pid)
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
        git_file = self.workspace / ".git"
        try:
            line = git_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"research workspace has no worktree metadata: {git_file}"
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
    """Dispatch the Proposer's non-terminal research actions.

    Command execution is delegated to ``ResearchCommandRunner``. All memory
    lookups (findings, experiments, episodes) go through ``MemoryService``.
    """

    def __init__(
        self,
        *,
        runtime,
        workspace: Path,
        repo: Path,
        history_dir: Path | None,
        scratch: Path,
        memory_service,
        command_timeout_seconds: int,
        command_output_cap_chars: int,
        current_round: int,
    ):
        self.memory = memory_service
        self.current_round = int(current_round)
        self.command_timeout_seconds = command_timeout_seconds
        self.command_runner = ResearchCommandRunner(
            runtime=runtime,
            workspace=workspace,
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
                    "result": self.memory.inspect_episode(action["ref"]),
                }
            if name == "list_findings":
                return {
                    "ok": True,
                    "result": self.memory.list_findings(
                        state=action.get("state", "active"),
                        limit=action.get("limit", 20),
                        current_round=self.current_round,
                    ),
                }
            if name == "search_findings":
                return {
                    "ok": True,
                    "result": self.memory.search_findings(
                        query=action["query"],
                        limit=action.get("limit", 5),
                    ),
                }
            if name == "inspect_finding":
                return {
                    "ok": True,
                    "result": self.memory.inspect_finding(
                        action["finding_id"],
                    ),
                }
            if name == "search_experiments":
                return {
                    "ok": True,
                    "result": self.memory.search_experiments(
                        query=action["query"],
                        filters=action.get("filters") or None,
                        limit=action.get("limit", 10),
                        buckets=bool(action.get("buckets", True)),
                    ),
                }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        raise ValueError(f"unsupported research action: {name}")
