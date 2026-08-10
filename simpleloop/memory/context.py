"""Build the two explicit Scientist context views.

Fresh inquiry receives only current-world facts. The history entry view is a
bounded factual index exposed only after portfolio commitment.
"""
from __future__ import annotations

import json



def build_fresh_inquiry_context(
    *,
    goal: str,
    editable: list[str],
    frozen: list[str],
    base_sha: str,
    gate_block: str,
    hints: list[str] | None = None,
) -> str:
    """Current-world evidence for a history-blind Scientist context."""
    hints_block = ""
    if hints:
        bullets = "\n".join(f"  - {hint}" for hint in hints)
        hints_block = (
            "\nGuidance (observations, not required directions):\n"
            f"{bullets}\n"
        )
    return f"""Research objective:
{goal}

{hints_block}
Harness Gates:
{gate_block or "(declared in factual records)"}

Current accepted revision: {base_sha}
Editable paths: {json.dumps(editable, ensure_ascii=False)}
Frozen paths: {json.dumps(frozen, ensure_ascii=False)}
"""


def build_history_entry_pack(*, experiments, tool_cheatsheet: str) -> str:
    """Expose a bounded factual index and retrieval tools, not a worldview."""
    rows = sorted(
        experiments or (), key=lambda exp: (exp.round, exp.candidate),
    )[-20:]
    index_lines: list[str] = []
    for exp in rows:
        gate = "pass" if exp.gate_passed else "fail"
        selected = " selected" if exp.selected else ""
        target = exp.finding_id or "-"
        scope = ",".join(exp.changed_paths) or "-"
        index_lines.append(
            f"  {exp.experiment_id} target={target} outcome={exp.status} "
            f"gate={gate}{selected} {_short_metrics(exp.metrics)} "
            f"changed_scope={scope}"
        )
    factual_index = "\n".join(index_lines) or "(no prior experiments)"
    tools = tool_cheatsheet or "(none)"
    return f"""History is now available as evidence.

Recent factual experiment index:
{factual_index}

Memory tools now available for on-demand retrieval:
{tools}

Use history to support, refute, or revise the independently formed model and
hypotheses. Do not replace your representation with historical vocabulary.
"""


def _short_metrics(metrics: dict) -> str:
    if not metrics:
        return "metrics=(none)"
    parts: list[str] = []
    for key, value in metrics.items():
        if isinstance(value, float):
            parts.append(f"{key}={value:g}")
        else:
            parts.append(f"{key}={value}")
    return "metrics=" + ", ".join(parts)
