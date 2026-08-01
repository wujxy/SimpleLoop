"""Read-only factual lookup and bounded Proposer Insight memory."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..harness.memory import resolve_episode


def search_history(
    history: list[dict], query: str, *, limit: int = 20,
) -> list[dict]:
    """Return compact factual matches in newest-first order."""
    needle = str(query).strip().casefold()
    if not needle:
        raise ValueError("history query must be non-empty")
    matches: list[dict] = []
    for record in reversed(history):
        for candidate in reversed(record.get("candidates") or []):
            ref = f"r{record['round']}c{candidate['candidate']}"
            index = {
                "ref": ref,
                "proposal": candidate.get("proposal"),
                "status": candidate.get("status"),
                "changed_paths": candidate.get("changed_paths") or [],
                "metric_keys": list((candidate.get("metrics") or {}).keys()),
                "gate_keys": list((candidate.get("gates") or {}).keys()),
            }
            if needle not in json.dumps(
                index, ensure_ascii=False,
            ).casefold():
                continue
            episode = resolve_episode(history, ref)
            matches.append({
                key: value for key, value in episode.items()
                if key != "eval_block"
            })
            if len(matches) == limit:
                return matches
    return matches


@dataclass(frozen=True)
class Insight:
    text: str
    refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("insight text must be non-empty")
        if len(self.text) > 500:
            raise ValueError("insight text must be at most 500 characters")
        if not self.refs or not all(isinstance(ref, str) for ref in self.refs):
            raise ValueError("insight refs must be a non-empty string list")

    def to_dict(self) -> dict:
        return {"text": self.text.strip(), "refs": list(self.refs)}

    @classmethod
    def from_dict(cls, value: dict) -> "Insight":
        if not isinstance(value, dict) or set(value) != {"text", "refs"}:
            raise ValueError("insight must contain only text and refs")
        refs = value["refs"]
        if not isinstance(refs, list):
            raise ValueError("insight refs must be a list")
        return cls(text=value["text"], refs=tuple(refs))


class InsightStore:
    """Append-only, one-record-per-round semantic memory."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"could not read insight memory {self.path}: {exc}"
            ) from exc
        seen: set[str] = set()
        for record in records:
            if not self._valid_record(record) or record["id"] in seen:
                raise ValueError(
                    f"insight memory {self.path} does not use the current "
                    "insight schema"
                )
            seen.add(record["id"])
        return records

    def append(self, round_id: int, insight: Insight) -> bool:
        record = {
            "id": f"I{round_id}",
            "round": round_id,
            **insight.to_dict(),
        }
        existing = next(
            (row for row in self.load() if row["id"] == record["id"]),
            None,
        )
        if existing == record:
            return False
        if existing is not None:
            raise ValueError(f"conflicting insight {record['id']}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True

    @staticmethod
    def _valid_record(record: object) -> bool:
        if not isinstance(record, dict) or set(record) != {
            "id", "round", "text", "refs",
        }:
            return False
        round_id = record["round"]
        if (not isinstance(round_id, int) or isinstance(round_id, bool)
                or round_id < 0 or record["id"] != f"I{round_id}"):
            return False
        try:
            Insight.from_dict({"text": record["text"], "refs": record["refs"]})
        except ValueError:
            return False
        return True


def render_insights(records: list[dict]) -> str:
    return json.dumps(records, ensure_ascii=False)


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
        payload = [
            "env",
            "GIT_DIR=/repo/.git",
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
        try:
            stdout, stderr = process.communicate(
                timeout=timeout,
            )
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            returncode = None
        output = stdout or ""
        if stderr:
            output += "\n[stderr]\n" + stderr
        truncated = len(output) > self.output_cap_chars
        output = output[:self.output_cap_chars]
        return {
            "ok": not timed_out and returncode == 0,
            "returncode": returncode,
            "timed_out": timed_out,
            "truncated": truncated,
            "output": output,
        }


class ResearchTools:
    """Dispatch the Proposer's four non-terminal research actions."""

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
        self.pending_insight: Insight | None = None
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
            if name == "search_history":
                return {
                    "ok": True,
                    "result": search_history(self.history, action["query"]),
                }
            if name == "inspect_episode":
                return {
                    "ok": True,
                    "result": resolve_episode(self.history, action["ref"]),
                }
            if name == "write_insight":
                insight = Insight.from_dict({
                    "text": action["text"], "refs": action["refs"],
                })
                for ref in insight.refs:
                    resolve_episode(self.history, ref)
                self.pending_insight = insight
                return {"ok": True, "result": "insight pending"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        raise ValueError(f"unsupported research action: {name}")
