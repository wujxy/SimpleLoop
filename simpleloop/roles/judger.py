"""Judger: grades one round's diff + eval output and returns score/risk/feedback.

The harness computes the diff, runs eval, and parses/computes all metrics and
deltas (an LLM asked to extract numbers from prose hallucinates them); the
judger only interprets the authoritative FACTS block it is handed."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent import Agent, normalize_free_text
from ..prompts import load_semantic
from ..harness import evals
from ..harness.workspace import Workspace


_STRUCTURED_TEXT_MARGIN = 500
_FEEDBACK_GENERATION_LIMIT = 500
_FEEDBACK_FOR_PROPOSER_GENERATION_LIMIT = 300


@dataclass
class Judgment:
    score: float
    risk: str                      # low|medium|high - latent-correctness risk of the refactor
    feedback: str                  # 300-500 chars: four-part diagnostic for proposer + final report
    feedback_for_proposer: str     # concise mechanism-level search context


def _judger_schema() -> dict:
    """Require strict structure while leaving free-text length to the parser."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "risk", "feedback", "feedback_for_proposer"],
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
                "pattern": r"\S",
            },
            "feedback_for_proposer": {
                "type": "string",
                "minLength": 1,
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
          label: str = "judger", prompt_dir: str | Path | None = None) -> Judgment:
    """Grade one round. Returns both full and proposer-facing feedback.

    All metrics/deltas passed in are harness-parsed; an empty eval_block means
    no commit was produced this round."""
    if sha is not None:
        diff = workspace.diff(parent_sha, sha)
    else:
        diff = f"(no commit produced this round: {reason})"

    prompt = _build_prompt(goal, proposal, diff, eval_block,
                           metrics, prior_metrics, baseline_metrics, metrics_schema, prompt_dir)
    data = agent.run_json(
        prompt,
        cwd=cwd,
        label=label,
        json_schema=_judger_schema(),
    )
    return _parse(data, label=label)


def _build_prompt(goal: str, proposal: str, diff: str, eval_block: str,
                  metrics: dict | None, prior_metrics: dict | None,
                  baseline_metrics: dict | None,
                  metrics_schema: dict | None, prompt_dir: str | Path | None = None) -> str:
    # The authoritative FACTS block is harness-computed; the judger cites its
    # numbers by name and does no extraction/arithmetic.
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

    semantic = load_semantic("judger", prompt_dir)
    return f"""{semantic}

Task goal:
{goal}

Direction that was attempted:
{proposal}

Change against the accepted parent:
```diff
{diff}
```

{eval_section}{facts_block}Fixed evidence protocol:
{eval_guidance}{facts_guidance}- The supplied diff, eval output, and harness facts are the available evidence.
- Numeric claims come from the authoritative metrics block.
- `risk` is one of `low`, `medium`, or `high` and describes latent correctness risk.
- `feedback` begins with `LANDED_STATE: <not-implemented|already-implemented|gate-rejected>` and factually describes implementation and result.
- `feedback_for_proposer` is compact search experience rather than a next-direction recommendation.

Return exactly one JSON object matching the supplied schema with score, risk,
feedback, and feedback_for_proposer.
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
    delta = evals.objective_delta(this, other, lower_is_better)
    if delta is None:
        return f"  Delta vs {axis:<8}: {key} unknown (prior value missing or zero)"
    pct, improved = delta
    tag = "improvement" if improved else ("regression" if pct != 0 else "no change")
    return f"  Delta vs {axis:<8}: {key} {pct:+.1f}%  ({tag})"


def _parse(data: dict, *, label: str = "judger") -> Judgment:
    required = {"score", "risk", "feedback", "feedback_for_proposer"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError(
            "judger response must contain only score, risk, feedback, and "
            "feedback_for_proposer")
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
    feedback_for_proposer = data["feedback_for_proposer"]
    if (
        not isinstance(feedback_for_proposer, str)
        or not feedback_for_proposer.strip()
    ):
        raise ValueError(
            "judger 'feedback_for_proposer' must be a non-empty string: "
            f"{data}"
        )
    return Judgment(
        score=score,
        risk=risk,
        feedback=normalize_free_text(
            feedback,
            limit=_FEEDBACK_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
            label=label,
            field="feedback",
        ),
        feedback_for_proposer=normalize_free_text(
            feedback_for_proposer,
            limit=(
                _FEEDBACK_FOR_PROPOSER_GENERATION_LIMIT
                + _STRUCTURED_TEXT_MARGIN
            ),
            label=label,
            field="feedback_for_proposer",
        ),
    )
