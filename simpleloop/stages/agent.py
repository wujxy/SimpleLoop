"""Thin Claude CLI adapter over a prepared World."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from ..world import ExecutionSandbox, ProcessRequest


class AgentError(RuntimeError):
    """Raised when the agent call fails or returns unparseable output."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


@dataclass
class AgentResult:
    text: str           # the agent's response text (inside the JSON envelope)
    data: dict          # parsed JSON object (run_json only)
    usage: object = None


def _decode_output(stdout: str) -> AgentResult:
    """Decode Claude's JSON envelope without interpreting usage semantics."""
    text = stdout
    data: dict = {}
    usage: object = None
    try:
        outer = json.loads(stdout)
        if isinstance(outer, dict):
            structured = outer.get("structured_output")
            if isinstance(structured, dict):
                data = structured
            if isinstance(outer.get("result"), str):
                text = outer["result"]
            usage = outer.get("usage")
    except json.JSONDecodeError:
        pass
    return AgentResult(text=text, data=data, usage=usage)


class Agent:
    def __init__(
        self,
        *,
        world: ExecutionSandbox,
        command: str = "claude",
        timeout_seconds: int = 1800,
        extra_args: list[str] | None = None,
        model: str | None = None,
        allowed_tools: str = "Read,Edit,Write,Bash",
        usage_observer: Callable[[object], None] | None = None,
    ):
        self.world = world
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        self.model = model
        self.allowed_tools = allowed_tools
        self.usage_observer = usage_observer

    def _notify_usage(self, usage: object, label: str) -> None:
        if self.usage_observer is None:
            return
        try:
            self.usage_observer(usage)
        except Exception as exc:
            print(
                f"[telemetry] warning: {label} usage was not recorded: {exc}",
                flush=True,
            )

    def run_json(
        self,
        prompt: str,
        *,
        cwd: Path,
        label: str = "agent",
        json_schema: dict,
    ) -> dict:
        """Run the agent with schema-enforced structured output; return the JSON object."""
        result = self._run(
            prompt, cwd=cwd, label=label, json_schema=json_schema)
        if result.data:
            return result.data
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise AgentError(
                f"[{label}] structured output was not an exact JSON object: {exc}",
                raw_output=result.text,
            ) from exc
        if not isinstance(parsed, dict):
            raise AgentError(
                f"[{label}] structured output must be a JSON object",
                raw_output=result.text,
            )
        return parsed

    def run_text(self, prompt: str, *, cwd: Path, label: str = "agent") -> str:
        """Run the agent, return its raw text. Used by the executor (no JSON expected)."""
        return self._run(prompt, cwd=cwd, label=label).text

    def _run(self, prompt: str, *, cwd: Path, label: str,
             json_schema: dict | None = None) -> AgentResult:
        # Prompt goes via STDIN, not argv: a long assembled prompt can exceed the
        # kernel's ARG_MAX and kill Popen mid-run.
        payload = [
            self.command, "-p",
            "--input-format", "text",
            "--output-format", "json",
            "--allowedTools", self.allowed_tools,
        ]
        if self.model:
            payload += ["--model", self.model]
        payload += self.extra_args
        if json_schema is not None:
            payload += [
                "--json-schema",
                json.dumps(json_schema, separators=(",", ":")),
            ]
        print(
            f"[{label}] claude call started "
            f"(timeout={self.timeout_seconds}s, world=/work)",
            flush=True,
        )
        completed = self.world.run(ProcessRequest(
            tuple(payload), PurePosixPath("/work"), self.timeout_seconds,
            stdin=prompt, label=label,
        ))
        result = _decode_output(completed.stdout)
        self._notify_usage(result.usage, label)
        if completed.timed_out:
            raise AgentError(
                f"[{label}] timed out after {self.timeout_seconds}s\n"
                f"stderr: {completed.stderr.strip()[:2000]}"
            )
        if completed.exit_code != 0:
            raise AgentError(
                f"[{label}] claude exited {completed.exit_code}\n"
                f"stdout: {completed.stdout.strip()[:2000]}\n"
                f"stderr: {completed.stderr.strip()[:2000]}"
            )
        print(
            f"[{label}] claude call finished "
            f"({completed.duration_seconds:.0f}s)", flush=True,
        )
        return result
