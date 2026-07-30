"""Thin Claude Code CLI adapter: run `claude -p` as a subprocess with timeout,
heartbeat logging and schema-enforced JSON output (run_json) or raw text (run_text)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..container.runtime import ApptainerRuntime


class AgentError(RuntimeError):
    """Raised when the agent call fails or returns unparseable output."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


def normalize_free_text(
    value: str,
    *,
    limit: int,
    label: str,
    field: str,
) -> str:
    """Strip free text and visibly truncate it at the local tolerance limit."""
    normalized = value.strip()
    length = len(normalized)
    if length > limit:
        print(
            f"[{label}] warning: {field} length {length} exceeds {limit}; "
            f"truncated to {limit}",
            flush=True,
        )
        return normalized[:limit]
    return normalized


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
        runtime: ApptainerRuntime,
        command: str = "claude",
        timeout_seconds: int = 1800,
        extra_args: list[str] | None = None,
        model: str | None = None,
        allowed_tools: str = "Read,Edit,Write,Bash",
        max_output_tokens: int = 64000,
        usage_observer: Callable[[object, str], None] | None = None,
    ):
        self.runtime = runtime
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        self.model = model
        self.allowed_tools = allowed_tools
        # Raised above Claude Code's 32000 default so long reasoning before the
        # final JSON does not abort the turn.
        self.max_output_tokens = max_output_tokens
        self.usage_observer = usage_observer

    def _notify_usage(self, usage: object, label: str) -> None:
        if self.usage_observer is None:
            return
        try:
            self.usage_observer(usage, label)
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
        argv = self.runtime.exec_argv(payload, cwd=cwd)

        prompt_bytes = prompt.encode("utf-8")
        print(f"[{label}] claude call started (timeout={self.timeout_seconds}s, cwd={cwd}, "
              f"prompt={len(prompt_bytes)}B via stdin)", flush=True)
        env = self.runtime.subprocess_env({
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_output_tokens),
        })
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        out_buf: list[str] = []
        err_buf: list[str] = []
        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_buf, label), daemon=True)
        t_out.start()
        t_err.start()

        # Write stdin on its own thread so a stuck pipe write cannot hold the
        # timeout loop hostage.
        write_err: list = []
        def _feed() -> None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                write_err.append(exc)
        t_feed = threading.Thread(target=_feed, daemon=True)
        t_feed.start()

        deadline = time.monotonic() + self.timeout_seconds
        heartbeat = time.monotonic() + 30.0
        while proc.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                _kill_group(proc)
                self._notify_usage(None, label)
                raise AgentError(
                    f"[{label}] timed out after {self.timeout_seconds}s\n"
                    f"stderr: {''.join(err_buf).strip()[:2000]}"
                )
            if now >= heartbeat:
                elapsed = deadline - now
                print(f"[{label}] still running ({self.timeout_seconds - elapsed:.0f}s in, pid={proc.pid})", flush=True)
                heartbeat = now + 30.0
            time.sleep(0.2)

        t_out.join(timeout=2)
        t_err.join(timeout=2)
        t_feed.join(timeout=2)
        if write_err:
            self._notify_usage(None, label)
            raise AgentError(f"[{label}] stdin write failed: {write_err[0]}\n"
                             f"stderr: {''.join(err_buf).strip()[:2000]}")
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        if proc.stdin:
            proc.stdin.close()
        stdout = "".join(out_buf)
        stderr = "".join(err_buf)
        elapsed = self.timeout_seconds - (deadline - time.monotonic())
        result = _decode_output(stdout)
        self._notify_usage(result.usage, label)

        if proc.returncode != 0:
            raise AgentError(
                f"[{label}] claude exited {proc.returncode}\n"
                f"stdout: {stdout.strip()[:2000]}\nstderr: {stderr.strip()[:2000]}"
            )
        print(f"[{label}] claude call finished ({elapsed:.0f}s)", flush=True)

        return result

def _drain(stream, buf: list[str], label: str | None = None) -> None:
    if stream is None:
        return
    for line in stream:
        buf.append(line)
        if label:
            print(f"[{label} stderr] {line.rstrip()}", flush=True)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
