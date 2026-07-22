"""Judger: looks at the diff + eval output, grades the round, gives feedback.

The harness (not the LLM) computes the diff, runs the eval commands, AND parses
the structured metrics out of eval output - these are deterministic and must
not be delegated to the agent. The loop runs eval in the worktree (the real
checked-out tree the executor committed) BEFORE calling the judger, parses
`KEY=VALUE` lines into a metrics dict, then passes both the raw text and the
parsed metrics in. The judger agent only judges: it sees goal + proposal + diff
+ an authoritative metrics block (computed by the harness) and returns a score,
a risk band, and feedback.

Why the metrics block is harness-computed, not judger-computed: an LLM asked to
extract numbers from prose and do arithmetic on them will hallucinate. A prior
run invented a "baseline 874.50" that appears in no ground-truth source and
propagated it across 12 rounds of feedback (see memory
simpleloop-judger-prior-round-compare). The fix: the harness parses the real
numbers, computes the deltas, and hands them to the judger as authoritative
facts. The judger may cite them but must not introduce any number not in the
block.

The judger runs with cwd = the worktree, so it may `cat`/`grep` the actual
committed source or re-run a command to verify a claim the eval headline makes.
That is verification, not the primary evidence - the metrics block is the
ground truth.

When there's no SHA (gate rejected / no change), the judger is still called but
shown the rejection reason instead of a diff, and asked for a low score + feedback
telling the proposer to avoid that direction.

Delivers: {"score": 0.0-1.0, "risk": "low"|"medium"|"high", "feedback": "..."}.
`feedback` begins with a `LANDED_STATE: <already-implemented|not-implemented|
gate-rejected>` tag so the proposer can self-audit whether a direction is already
landed without guessing from prose. The judger does NOT propose the next
direction - it gives effect + landing-state facts only; direction choice is the
proposer's job.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from .workspace import Workspace


_STRUCTURED_TEXT_MARGIN = 300
_FEEDBACK_GENERATION_LIMIT = 500


@dataclass
class Judgment:
    score: float
    risk: str                      # low|medium|high - latent-correctness risk of the refactor
    feedback: str                  # 300-500 chars: four-part diagnostic for proposer + final report


def _judger_schema() -> dict:
    """Keep strict structure while accepting N+300 feedback headroom."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "risk", "feedback"],
        "properties": {
            "score": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "risk": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "feedback": {
                "type": "string",
                "minLength": 1,
                "maxLength": _FEEDBACK_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
                "pattern": r"\S",
            },
        },
    }


def judge(agent: Agent, *, goal: str, proposal: str, sha: str | None,
          reason: str | None, parent_sha: str, workspace: Workspace,
          eval_block: str, cwd: Path,
          metrics: dict | None = None,
          prior_metrics: dict | None = None,
          baseline_metrics: dict | None = None,
          metrics_schema: dict | None = None,
          label: str = "judger") -> Judgment:
    """Grade one round. Returns Judgment(score, risk, feedback).

    eval_block is the harness-run eval output (run in the worktree before this
    call). metrics/prior_metrics/baseline_metrics are the harness-parsed
    key=value dicts for this round / the prior round / the baseline commit,
    paired with metrics_schema (the config-declared objective+gates) so the
    harness can compute the authoritative deltas the judger cites.

    The judger does NOT run eval, parse numbers, or compute deltas - all of
    that is harness-owned. Empty eval_block / None metrics means either no eval
    configured (diff-only judger) or no commit. See the hallucination note in the
    module docstring.
    """
    if sha is not None:
        diff = workspace.diff(parent_sha, sha)
    else:
        diff = f"(no commit produced this round: {reason})"

    prompt = _build_prompt(goal, proposal, diff, eval_block,
                           metrics, prior_metrics, baseline_metrics, metrics_schema)
    data = agent.run_json(
        prompt,
        cwd=cwd,
        label=label,
        json_schema=_judger_schema(),
    )
    return _parse(data)


def _build_prompt(goal: str, proposal: str, diff: str, eval_block: str,
                  metrics: dict | None, prior_metrics: dict | None,
                  baseline_metrics: dict | None,
                  metrics_schema: dict | None) -> str:
    # ---- the authoritative FACTS block (harness-computed) -------------------
    # Replaces the old approach of handing the judger raw prior/baseline eval
    # TEXT and asking it to read the number out - that's where the 874.50
    # hallucination came from. Now the harness parses the numbers and computes
    # the deltas; the judger cites them by name and does no extraction/arithmetic.
    facts_block = ""
    facts_guidance = ""
    if metrics_schema and (metrics or prior_metrics or baseline_metrics):
        lines = [_facts_header(), _facts_row("THIS round", metrics, metrics_schema)]
        if prior_metrics:
            lines.append(_facts_row("PRIOR round", prior_metrics, metrics_schema))
        if baseline_metrics:
            lines.append(_facts_row("BASELINE   ", baseline_metrics, metrics_schema))
        # deltas on the objective only (gates are pass/fail, no delta)
        obj_key = metrics_schema["objective"]["key"]
        lib = metrics_schema["objective"]["lower_is_better"]
        deltas = []
        if metrics and obj_key in metrics and prior_metrics and obj_key in prior_metrics:
            deltas.append(_delta_line(obj_key, "prior",
                                       metrics[obj_key], prior_metrics[obj_key], lib))
        if metrics and obj_key in metrics and baseline_metrics and obj_key in baseline_metrics:
            deltas.append(_delta_line(obj_key, "baseline",
                                       metrics[obj_key], baseline_metrics[obj_key], lib))
        if deltas:
            lines.append("  --- deltas (harness-computed) ---")
            lines.extend(deltas)
        facts_block = "\n".join(lines) + "\n\n"
        facts_guidance = (
            "- The metrics block above is AUTHORITATIVE - computed by the harness from the "
            "eval output. These are the ONLY numbers you may cite. If a metric is missing "
            "(shown as 'unknown' or absent), say it is unknown; do NOT estimate or invent it. "
            "Do not recompute the deltas; they are given.\n"
            "- Your job here is QUALITATIVE: is the refactor clean and bit-faithful, does it "
            "introduce latent correctness risk (name it concretely - e.g. a cache keyed on "
            "too few inputs, a cached pointer that may be null on an untested code path), "
            "does this round deserve credit vs the prior round. Cite metrics by name.\n"
        )

    # ---- raw eval text (still provided for verification of a claim) ---------
    has_eval = bool(eval_block)
    eval_section = f"""Eval command output (THIS round - raw text, for verifying a specific claim):
{eval_block}
""" if has_eval else ""
    eval_guidance = (
        "- If the eval command failed (non-zero exit - visible in the raw text, or a gate "
        "metric shows FAIL), cap the score at 0.50 and say so in feedback. The metrics block's "
        "gate values are authoritative for pass/fail.\n"
        "- Reward a real improvement visible in the metrics block; do not credit an improvement "
        "that is only asserted in the diff without a metrics change.\n"
    ) if has_eval else (
        "- There is no eval output this round; judge on the diff alone. Do not claim a measured "
        "improvement you cannot see - cap the score at 0.70 unless the diff clearly shows a "
        "correct, low-risk improvement.\n"
    )

    return f"""You are the JUDGER in a serial optimization loop. Grade this round's change.

Your scope: you see ONE round's diff + metrics. You judge the effect and tag a landing state. You do NOT choose the next direction - leave that to the proposer. Your feedback is a reference for the proposer, not an instruction.

Task goal:
{goal}

Direction that was attempted:
{proposal}

Change (git diff vs the previous round's result):
```diff
{diff}
```
{eval_section}{facts_block}Scoring rubric (score 0.0 to 1.0):
- 0.90-1.00: clearly exceeds the goal - real measured improvement (in metrics) with no regressions and clean code.
- 0.70-0.90: solid improvement in the right direction, low risk, code still correct.
- 0.50-0.70: directionally useful but modest - small gain, or gain without metrics proof, or minor risk.
- 0.30-0.50: weak / inconclusive - change happened but unclear benefit, or validation incomplete.
- 0.10-0.30: poor - wrong direction, introduced risk, broke a gate, or mostly duplicate work.
- 0.00-0.10: failed - no real change, broken code, or touched something it shouldn't.

Judging guidance:
- Judge whether the change moves toward the goal, achieves real improvement, introduces risk, and is good-quality code.
{eval_guidance}{facts_guidance}- Penalize unsupported claims, regressions vs the prior round, and changes that break a gate.
- Give objective facts about what landed, how it measured, and why it behaved that way. Do not choose the next direction.
- `risk` is your read of the refactor's LATENT correctness risk (not the measured speed - the harness owns speed for best selection): 'high' if the change plausibly breaks on inputs the eval didn't exercise (e.g. a cache keyed on too few state vars, a cached null pointer on an untested branch, arithmetic that drifted); 'medium' if there's a caveat worth flagging but no clear break; 'low' if the refactor is a clean bit-faithful move with the same operators/evaluation order/types. Be concrete in feedback about WHY the risk level.

Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object with three keys:
  {{"score": <0.0-1.0>, "risk": "<low|medium|high>", "feedback": "<300-500 chars, four-part>"}}
- A ```json code fence is acceptable; any prose, heading, commentary, or natural-language summary outside the JSON is forbidden.
- `feedback` must be 300-500 chars, four labeled parts (1-2 sentences each):
  LANDING_STATE: <not-implemented|already-implemented|gate-rejected>
  Implemented: <one sentence - what the executor actually changed; state plainly if partial/no-op/gate-rejected/drifted>
  Result: <one sentence - the key metric + vs-prior delta + gate pass/fail>
  Analysis: <two sentences - why it succeeded/regressed/failed, and the key heuristic; do not pick the next direction>
- LANDING_STATE: `not-implemented` (a real diff/commit this round), `already-implemented` (executor made no change, reason="executor made no changes"), `gate-rejected` (changes made but frozen_paths gate rejected them).
- Do not suggest the next direction; give current-state diagnostics only.
- If evidence is incomplete or contradictory, still return the JSON object with a low score and explain the uncertainty in `feedback`.
"""


def _facts_header() -> str:
    return ("Authoritative metrics (parsed+computed by the harness - the ONLY numbers "
            "you may cite; do not introduce or estimate any metric not listed here):")


def _facts_row(label: str, metrics: dict | None, schema: dict) -> str:
    """One row: 'THIS round:  SPEED_MS = 571.79  |  CORRECTNESS = PASS'."""
    if not metrics:
        return f"  {label}: (unknown)"
    parts = []
    obj_key = schema["objective"]["key"]
    parts.append(f"{obj_key} = {_fmt_val(metrics.get(obj_key))}")
    for g in schema.get("gates", []):
        gk = g["key"]
        parts.append(f"{gk} = {_fmt_val(metrics.get(gk))}")
    return f"  {label}:  " + "  |  ".join(parts)


def _fmt_val(v) -> str:
    if v is None:
        return "unknown"
    if isinstance(v, bool):
        return "PASS" if v else "FAIL"
    return str(v)


def _delta_line(key: str, axis: str, this, other, lower_is_better: bool) -> str:
    """'Delta vs prior:   SPEED_MS -30.5%  (improvement)'."""
    if not isinstance(this, (int, float)) or not isinstance(other, (int, float)) or other == 0:
        return f"  Delta vs {axis:<8}: {key} unknown (prior value missing or zero)"
    pct = (this - other) / other * 100.0
    improved = (pct < 0) if lower_is_better else (pct > 0)
    tag = "improvement" if improved else ("regression" if pct != 0 else "no change")
    sign = f"{pct:+.1f}%"
    return f"  Delta vs {axis:<8}: {key} {sign}  ({tag})"


def _parse(data: dict) -> Judgment:
    if not isinstance(data, dict) or set(data) != {"score", "risk", "feedback"}:
        raise ValueError(
            "judger response must contain only score, risk, and feedback")
    raw_score = data["score"]
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise ValueError(f"judger 'score' must be a number 0-1: {data}")
    score = float(raw_score)
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"judger 'score' out of range [0,1]: {score}")
    risk = data["risk"]
    if not isinstance(risk, str):
        raise ValueError(f"judger 'risk' must be low|medium|high, got: {risk!r}")
    if risk not in ("low", "medium", "high"):
        raise ValueError(f"judger 'risk' must be low|medium|high, got: {risk!r}")
    feedback = data["feedback"]
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError(f"judger 'feedback' must be a non-empty string (300-500 chars, four-part): {data}")
    return Judgment(
        score=score,
        risk=risk,
        feedback=feedback.strip()[
            :_FEEDBACK_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN
        ],
    )


def run_eval(commands: list[str], cwd: Path,
             metrics_schema: dict | None = None) -> tuple[str, dict]:
    _OUT_CAP = 16000
    blocks: list[str] = []
    full_text: list[str] = []
    for cmd in commands:
        completed = subprocess.run(
            cmd, shell=True, cwd=str(cwd),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )
        out = completed.stdout.strip()
        err = completed.stderr.strip()
        status = "OK" if completed.returncode == 0 else f"EXIT {completed.returncode}"
        body = out if out else err
        blocks.append(f"$ {cmd}  [{status}]\n{body[:_OUT_CAP]}")
        full_text.append(body)
    text = "\n\n".join(blocks)
    combined = "\n".join(full_text)
    metrics = _parse_metrics(combined, metrics_schema) if metrics_schema else {}
    return text, metrics


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
