"""Harness-owned eval execution and metric parsing.

The harness (not any LLM role) runs the configured eval commands and parses the
structured metrics out of their output — these are deterministic and must not
be delegated to an agent. The loop runs eval in the worktree (the real
checked-out tree the executor committed) BEFORE calling the judger, parses
`KEY=VALUE` lines into a metrics dict, then passes both the raw text and the
parsed metrics to the judger.

Why the metrics are harness-computed, not judger-computed: an LLM asked to
extract numbers from prose and do arithmetic on them will hallucinate. A prior
run invented a "baseline 874.50" that appears in no ground-truth source and
propagated it across 12 rounds of feedback (see memory
simpleloop-judger-prior-round-compare). The fix: the harness parses the real
numbers, computes the deltas, and hands them to the judger as authoritative
facts.

This module lives beside (not inside) judger.py on purpose: judger.py is an
LLM role, this is deterministic harness machinery.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .runtime import ApptainerRuntime


@dataclass(frozen=True)
class EvalResult:
    """Captured harness output, parsed metrics, and per-command statuses."""

    text: str
    metrics: dict
    returncodes: tuple[int, ...]

    @property
    def commands_ok(self) -> bool:
        return all(code == 0 for code in self.returncodes)


def run_eval(
    commands: list[str],
    cwd: Path,
    runtime: ApptainerRuntime,
    metrics_schema: dict | None = None,
) -> EvalResult:
    _OUT_CAP = 16000
    blocks: list[str] = []
    full_text: list[str] = []
    returncodes: list[int] = []
    for cmd in commands:
        # Non-login bash: a login shell sources /etc/profile.d/*.sh, which on
        # EL-based images re-exports the `which` (and module/scl) function via
        # `export -f`, producing BASH_FUNC_which%% env vars that then poison
        # every /bin/sh child (SNiPER/Python) with "syntax error: unexpected
        # end of file" and break DLL load. Eval commands are expected to
        # source their own environment (e.g. sl_eval_post_v107.sh sources the
        # JUNO setup), so a login shell is unnecessary here.
        argv = runtime.exec_argv(["bash", "-c", cmd], cwd=cwd)
        completed = subprocess.run(
            argv, shell=False, cwd=str(cwd), env=runtime.subprocess_env(),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
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
        blocks.append(f"$ {cmd}  [{status}]\n{body[:_OUT_CAP]}")
        full_text.append(metric_source)
        returncodes.append(completed.returncode)
    text = "\n\n".join(blocks)
    combined = "\n".join(full_text)
    metrics = _parse_metrics(combined, metrics_schema) if metrics_schema else {}
    return EvalResult(text, metrics, tuple(returncodes))


def _parse_metrics(text: str, schema: dict | None) -> dict:
    """Parse declared key=value lines out of eval output. Returns a dict.

    For each declared objective/gate key, find a line matching ^<KEY>=<value>
    (case-insensitive key, value = token up to first whitespace). Numbers become
    float; gate tokens (PASS/FAIL/ok/...) kept as a normalized bool: True=PASS,
    False=FAIL, None if not a recognizable gate token (kept as the raw string).

    sl_eval.sh already prints exactly this shape:
        CORRECTNESS=PASS
        SPEED_MS=843.66630  ms/evt (10 events)
        EVAL_RESULT=ok
    so parsing the real OMILREC output is zero-friction.

    A key absent from the output is absent from the dict - downstream treats
    absent as "unknown", never as a defaulted/hallucinated placeholder.
    """
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

    # one regex per key, anchored to a line start, key=value-tokenthenspace
    out: dict = {}
    for key, role in keys:
        # match KEY= at start of a line, then a non-space value token
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
            # non-numeric objective (e.g. "NA" on a failed/crashed round) - treat
            # as unknown (omit) rather than keeping the raw string, so the FACTS
            # block shows "unknown" and best-selection skips it, instead of a
            # judger-citeable "NA" that reads like a value.
            continue
        else:  # gate
            out[key] = _gate_to_bool(raw_val)
    return out


def _gate_to_bool(token: str):
    """Normalize a gate value token to True (pass) / False (fail) / None (unknown).

    Handles exact tokens (PASS/FAIL, pass/fail, ok, 0/1, true/false) and
    failure-shaped tokens where the script jams a reason onto the value
    (e.g. 'correctness_fail', 'build_fail', 'bench_fail') - any token
    containing 'fail' is a failure. 'NA'/empty/unrecognized -> None (unknown),
    which the best-selector treats as gate-not-passed (never a pass).
    """
    t = token.strip().lower()
    if t in ("", "na", "n/a", "none", "null"):
        return None
    if t in ("pass", "passed", "ok", "true", "1", "yes", "success"):
        return True
    if "fail" in t or t in ("false", "0", "no", "error", "err", "broken"):
        return False
    return None  # unrecognized token - don't guess
