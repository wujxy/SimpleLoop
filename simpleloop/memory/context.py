"""Build the two explicit Scientist context views.

Fresh inquiry receives only current-world facts. The history entry view is a
bounded factual index exposed only after portfolio commitment.
"""
from __future__ import annotations




def build_fresh_inquiry_context(
    *,
    goal: str,
    editable: list[str] | None = None,
    frozen: list[str] | None = None,
    base_sha: str | None = None,
    gate_block: str,
    hints: list[str] | None = None,
) -> str:
    """Return only task outcomes and the generic workspace capability."""
    return f"""Research objective:
{goal}

Harness Gates:
{gate_block or "(declared in factual records)"}

Workspace:
The provided workspace contains the complete mutable production artifact.
Its current structure is only the starting implementation, not part of the specification.
All task-specific prior knowledge available to you has been placed in this workspace.
You may inspect any file under /work.

Evaluation:
Success is determined only by the stated goal and gates. Evaluation is external
to the workspace; do not infer structural requirements beyond those criteria.
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
