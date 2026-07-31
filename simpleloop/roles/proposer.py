"""Open Researcher: investigate evidence and choose executable experiments."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from ..harness import views
from ..prompts import load_semantic


@dataclass
class ProposalBatch:
    proposals: list[str]


def _proposer_schema(candidates_per_round: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["proposals"],
        "properties": {
            "proposals": {
                "type": "array",
                "minItems": candidates_per_round,
                "maxItems": candidates_per_round,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"\S",
                },
            },
        },
    }


def propose(
    agent: Agent,
    *,
    goal: str,
    editable: list[str],
    frozen: list[str],
    history: list[dict],
    base_sha: str,
    cwd: Path,
    candidates_per_round: int = 1,
    recent_rounds: int = views._PROPOSER_RECENT_ROUNDS_DEFAULT,
    gate_block: str = "",
    prompt_dir: str | Path | None = None,
) -> ProposalBatch:
    """Return exactly K free-form experiment instructions."""
    visible = views.for_proposer(history, recent_rounds=recent_rounds)
    history_block = (
        json.dumps(visible, ensure_ascii=False, indent=2)
        if visible
        else "(none yet — this is the first round)"
    )
    semantic = load_semantic("proposer", prompt_dir)
    prompt = f"""{semantic}

Task goal:
{goal}

Gates:
{gate_block}

Current accepted revision:
- base_sha: {base_sha}

Previous factual outcomes:
{history_block}

Available evidence:
- The accepted revision and factual experiment index are starting points.
- Historical candidate SHAs and persisted run artifacts are available for read-only investigation.

Fixed boundaries:
- Generate exactly {candidates_per_round} executable experiment instructions.
- Every candidate starts from the accepted revision above.
- Editable paths: {editable}
- Frozen paths: {frozen}
- Return one JSON object containing only proposals, an array of nonblank strings.
"""
    data = agent.run_json(
        prompt,
        cwd=cwd,
        label="proposer",
        json_schema=_proposer_schema(candidates_per_round),
    )
    return _parse_batch(data, candidates_per_round=candidates_per_round)


def _parse_batch(data: dict, *, candidates_per_round: int) -> ProposalBatch:
    if not isinstance(data, dict):
        raise ValueError("proposer response must be an object")
    if set(data) != {"proposals"}:
        raise ValueError("proposer response must contain only proposals")
    proposals = data.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("proposals must be a list")
    if len(proposals) != candidates_per_round:
        raise ValueError(
            f"expected exactly {candidates_per_round} proposals, "
            f"got {len(proposals)}"
        )
    normalized: list[str] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, str) or not proposal.strip():
            raise ValueError(
                f"proposals[{index}] must be a non-empty string"
            )
        normalized.append(proposal.strip())
    return ProposalBatch(proposals=normalized)
