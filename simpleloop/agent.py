"""Thin Claude Code CLI adapter.

Calls `claude -p <prompt> --output-format json` as a non-interactive subprocess
with a timeout and heartbeat logging. Two call modes:

  - run_json(prompt): for proposer/judger. Parses the agent's text response as
    a JSON object; raises AgentError if it can't.
  - run_text(prompt): for executor. Returns the raw text; the executor does not
    return JSON, it just edits files in the worktree. We only care that it ran.

The cwd passed to run() is the worktree path, so the agent edits the right tree.
"""
from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


class AgentError(RuntimeError):
    """Raised when the agent call fails or returns unparseable output."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


@dataclass
class AgentResult:
    text: str           # the agent's response text (inside the JSON envelope)
    data: dict          # parsed JSON object (run_json only)


class Agent:
    def __init__(
        self,
        command: str = "claude",
        timeout_seconds: int = 1800,
        extra_args: list[str] | None = None,
        model: str | None = None,
        allowed_tools: str = "Read,Edit,Write,Bash",
    ):
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        self.model = model
        self.allowed_tools = allowed_tools

    def run_json(self, prompt: str, *, cwd: Path, label: str = "agent") -> dict:
        """Run the agent, return its parsed JSON object. Raises AgentError on failure."""
        result = self._run(prompt, cwd=cwd, label=label)
        try:
            return _extract_json(result.text)
        except AgentError as exc:
            exc.raw_output = result.text
            raise

    def run_text(self, prompt: str, *, cwd: Path, label: str = "agent") -> str:
        """Run the agent, return its raw text. Used by the executor (no JSON expected)."""
        return self._run(prompt, cwd=cwd, label=label).text

    def _run(self, prompt: str, *, cwd: Path, label: str) -> AgentResult:
        exe = self._resolve_command()
        argv = [
            exe, "-p", prompt,
            "--output-format", "json",
            "--allowedTools", self.allowed_tools,
        ]
        if self.model:
            argv += ["--model", self.model]
        argv += self.extra_args

        print(f"[{label}] claude call started (timeout={self.timeout_seconds}s, cwd={cwd})", flush=True)
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        out_buf: list[str] = []
        err_buf: list[str] = []
        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_buf, label), daemon=True)
        t_out.start()
        t_err.start()

        deadline = time.monotonic() + self.timeout_seconds
        heartbeat = time.monotonic() + 30.0
        while proc.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                _kill_group(proc)
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
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        stdout = "".join(out_buf)
        stderr = "".join(err_buf)
        elapsed = self.timeout_seconds - (deadline - time.monotonic())

        if proc.returncode != 0:
            raise AgentError(
                f"[{label}] claude exited {proc.returncode}\n"
                f"stdout: {stdout.strip()[:2000]}\nstderr: {stderr.strip()[:2000]}"
            )
        print(f"[{label}] claude call finished ({elapsed:.0f}s)", flush=True)

        # Claude Code's --output-format json wraps the agent text in a "result" field.
        text = stdout
        try:
            outer = json.loads(stdout)
            if isinstance(outer, dict) and isinstance(outer.get("result"), str):
                text = outer["result"]
        except json.JSONDecodeError:
            pass  # older/fake wrappers print the agent text directly
        return AgentResult(text=text, data={})

    def _resolve_command(self) -> str:
        found = shutil.which(self.command)
        if found:
            return found
        p = Path(self.command).expanduser()
        if p.is_absolute() or "/" in self.command:
            if p.exists():
                return str(p)
        raise AgentError(
            f"claude command not found: {self.command!r}. Install/authenticate "
            "Claude Code or set agent.command to an absolute path."
        )


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


def _extract_json(text: str) -> dict:
    """Extract the first JSON object from text (handles ```json fences)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AgentError(f"agent did not return a JSON object. raw:\n{text[:3000]}")
