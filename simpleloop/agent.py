"""Thin Claude Code CLI adapter.

Calls `claude -p --input-format text --output-format json` as a non-interactive
subprocess with a timeout and heartbeat logging. Two call modes:

  - run_json(prompt): for proposer/judger. With json_schema it requires Claude's
    structured output; without one it keeps the legacy tolerant JSON parser.
  - run_text(prompt): for executor. Returns the raw text; the executor does not
    return JSON, it just edits files in the worktree. We only care that it ran.

The prompt is fed via STDIN (not argv) so a long proposer history (kilobytes per
round, tens of rounds) cannot hit the kernel's ARG_MAX ceiling that killed a
prior run mid-loop with `OSError: [Errno 7] Argument list too long`.

The cwd passed to run() is the worktree path (executor/judger) or the per-run
repo (proposer), so the agent operates on the right tree/repo.
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
from typing import Callable


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
        command: str = "claude",
        timeout_seconds: int = 1800,
        extra_args: list[str] | None = None,
        model: str | None = None,
        allowed_tools: str = "Read,Edit,Write,Bash",
        max_output_tokens: int = 64000,
        usage_observer: Callable[[object], None] | None = None,
    ):
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        self.model = model
        self.allowed_tools = allowed_tools
        # Claude Code caps a single turn's output at 32000 tokens by default; a
        # proposer/judger that thinks hard (long reasoning before the final JSON)
        # can hit that ceiling and abort with "Claude's response exceeded the
        # 32000 output token maximum". Raise it so thinking + output have room.
        # Passed to the subprocess as CLAUDE_CODE_MAX_OUTPUT_TOKENS (the env var
        # claude reads), inheriting the rest of the host env.
        self.max_output_tokens = max_output_tokens
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
        json_schema: dict | None = None,
    ) -> dict:
        """Run the agent, return its parsed JSON object. Raises AgentError on failure."""
        result = self._run(
            prompt, cwd=cwd, label=label, json_schema=json_schema)
        if json_schema is not None:
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
        try:
            return _extract_json(result.text)
        except AgentError as exc:
            exc.raw_output = result.text
            raise

    def run_text(self, prompt: str, *, cwd: Path, label: str = "agent") -> str:
        """Run the agent, return its raw text. Used by the executor (no JSON expected)."""
        return self._run(prompt, cwd=cwd, label=label).text

    def _run(self, prompt: str, *, cwd: Path, label: str,
             json_schema: dict | None = None) -> AgentResult:
        exe = self._resolve_command()
        # Prompt goes to claude via STDIN, not argv. A long proposer history (the
        # round-N proposal can be kilobytes; across 14+ rounds the assembled
        # prompt grew past the kernel's ARG_MAX (~128KB for a single execve arg)
        # and Popen raised `OSError: [Errno 7] Argument list too long`, killing the
        # run mid-loop. With --input-format text (the default), `claude -p` reads
        # the prompt from stdin when no positional prompt arg is given — stdin is
        # unbounded by ARG_MAX (it's a pipe, not execve argv).
        argv = [
            exe, "-p",
            "--input-format", "text",
            "--output-format", "json",
            "--allowedTools", self.allowed_tools,
        ]
        if self.model:
            argv += ["--model", self.model]
        argv += self.extra_args
        if json_schema is not None:
            argv += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]

        prompt_bytes = prompt.encode("utf-8")
        print(f"[{label}] claude call started (timeout={self.timeout_seconds}s, cwd={cwd}, "
              f"prompt={len(prompt_bytes)}B via stdin)", flush=True)
        env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_output_tokens)}
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

        # Feed the prompt on stdin then close it; claude reads to EOF and answers.
        # A write to a pipe whose read end is a long-running agent could block until
        # the agent drains it, so write on a short-lived thread and never let a
        # stuck write hold the timeout loop hostage.
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
            # The agent died before/while we wrote stdin — surface it (returncode
            # will be nonzero and stderr will carry the real cause too, but be
            # explicit so the ARG_MAX-class failure is obvious if it ever recurs).
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
