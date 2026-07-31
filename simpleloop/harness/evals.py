"""Harness-owned eval execution and authoritative `KEY=VALUE` parsing."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..container.runtime import ApptainerRuntime


@dataclass(frozen=True)
class EvalResult:
    """Captured harness output, parsed metrics, and per-command statuses."""

    text: str
    metrics: dict
    returncodes: tuple[int, ...]

    @property
    def commands_ok(self) -> bool:
        return all(code == 0 for code in self.returncodes)


def objective_delta(this, other, lower_is_better: bool) -> tuple[float, bool] | None:
    """(pct_change, improved) of `this` vs `other`, or None when not computable."""
    if not isinstance(this, (int, float)) or not isinstance(other, (int, float)) or other == 0:
        return None
    pct = (this - other) / other * 100.0
    improved = (pct < 0) if lower_is_better else (pct > 0)
    return pct, improved


def run_eval(
    commands: list[str],
    cwd: Path,
    runtime: ApptainerRuntime,
    metrics_schema: dict | None = None,
    timeout_seconds: int = 600,
    output_cap: int = 16000,
) -> EvalResult:
    blocks: list[str] = []
    full_text: list[str] = []
    returncodes: list[int] = []
    for cmd in commands:
        # Non-login bash: a login shell's /etc/profile.d exports BASH_FUNC_*
        # vars that poison /bin/sh children on EL-based images.
        argv = runtime.exec_argv(["bash", "-c", cmd], cwd=cwd)
        completed = subprocess.run(
            argv, shell=False, cwd=str(cwd), env=runtime.subprocess_env(),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_seconds, check=False,
        )
        out = completed.stdout.strip()
        err = completed.stderr.strip()
        status = "OK" if completed.returncode == 0 else f"EXIT {completed.returncode}"
        if out and err:
            body = f"stdout:\n{out}\nstderr:\n{err}"
            metric_source = f"{out}\n{err}"
        else:
            body = out or err
            metric_source = body
        blocks.append(f"$ {cmd}  [{status}]\n{body[:output_cap]}")
        full_text.append(metric_source)
        returncodes.append(completed.returncode)
    text = "\n\n".join(blocks)
    combined = "\n".join(full_text)
    metrics = _parse_metrics(combined, metrics_schema) if metrics_schema else {}
    return EvalResult(text, metrics, tuple(returncodes))


def _parse_metrics(text: str, schema: dict | None) -> dict:
    """Parse declared `^KEY=value` lines out of eval output. Objectives become
    float, gates a normalized bool; a key absent from the output stays absent
    (downstream treats absent as unknown, never a defaulted placeholder)."""
    if not schema:
        return {}
    keys: list[tuple[str, str]] = []  # (key, role) - role only matters for typing
    obj = schema.get("objective", {})
    if obj.get("key"):
        keys.append((obj["key"], "objective"))
    for g in schema.get("gates", []):
        if g.get("key"):
            keys.append((g["key"], "gate"))
    if not keys:
        return {}

    out: dict = {}
    for key, role in keys:
        pat = re.compile(rf"(?m)^\s*{re.escape(key)}\s*=\s*(\S+)")
        m = pat.search(text)
        if not m:
            continue  # absent - stay unknown
        raw_val = m.group(1)
        if role == "objective":
            try:
                out[key] = float(raw_val)
                continue
            except ValueError:
                pass
            # Non-numeric objective (e.g. "NA") -> unknown, not a citeable string.
            continue
        else:  # gate
            out[key] = _gate_to_bool(raw_val)
    return out


def _gate_to_bool(token: str):
    """Normalize a gate value token to True (pass) / False (fail) / None (unknown);
    any token containing 'fail' is a failure, unknown is never a pass."""
    t = token.strip().lower()
    if t in ("", "na", "n/a", "none", "null"):
        return None
    if t in ("pass", "passed", "ok", "true", "1", "yes", "success"):
        return True
    if "fail" in t or t in ("false", "0", "no", "error", "err", "broken"):
        return False
    return None  # unrecognized token - don't guess
