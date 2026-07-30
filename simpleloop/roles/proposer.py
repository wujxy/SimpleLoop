"""Proposer: reads the per-run repo (read-only) + history and proposes the next
candidate directions. It never edits files; the executor implements in a worktree."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent import Agent, normalize_free_text
from ..prompts import load_semantic
from ..harness import views
from ..harness import memory as memory_mod


_STRUCTURED_TEXT_MARGIN = 500
_REFLECTION_GENERATION_LIMIT = 600
_INSIGHT_GENERATION_LIMIT = 500
_INSIGHT_REF_GENERATION_LIMIT = 32
_FAMILY_GENERATION_LIMIT = 64
_PROPOSAL_GENERATION_LIMIT = 800


@dataclass
class Proposal:
    """One candidate direction; only `proposal` is consumed by the executor,
    the rest is stored in history.jsonl as audit evidence."""
    proposal: str
    decision: str = "switch"   # default switch (conservative) if the judger omitted it
    reflection: str = ""
    family: str = "single"


@dataclass
class ProposalBatch:
    """One round's candidate directions plus the shared batch-level reflection."""
    reflection: str
    insight: str
    insight_refs: list[str]
    proposals: list[Proposal]


def _proposer_schema(candidates_per_round: int) -> dict:
    """Require exact structure while leaving free-text length to the parser."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reflection", "insight", "insight_refs", "proposals"],
        "properties": {
            "reflection": {
                "type": "string",
            },
            "insight": {
                "type": "string",
            },
            "insight_refs": {
                "type": "array",
                "items": {
                    "type": "string",
                },
            },
            "proposals": {
                "type": "array",
                "minItems": candidates_per_round,
                "maxItems": candidates_per_round,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["family", "decision", "proposal"],
                    "properties": {
                        "family": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": r"\S",
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["continue", "switch"],
                        },
                        "proposal": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": r"\S",
                        },
                    },
                },
            },
        },
    }


def propose(agent: Agent, *, goal: str, editable: list[str], frozen: list[str],
            history: list[dict], insights: list[dict], base_sha: str, cwd: Path,
            candidates_per_round: int = 1,
            recent_rounds: int = views._PROPOSER_RECENT_ROUNDS_DEFAULT,
            gate_block: str = "", prompt_dir: str | Path | None = None) -> ProposalBatch:
    """Return ProposalBatch for the next round."""
    # History is projected through the proposer's view (no raw eval_block).
    visible = views.for_proposer(history, recent_rounds=recent_rounds)
    insights_block = memory_mod.render_insights(insights)
    if visible:
        hist_lines = []
        for r in visible:
            cand_lines = []
            for c in r.get("candidates") or []:
                cm = c.get("metrics") or {}
                cm_parts = [f"{k}={v}" for k, v in cm.items()] if cm else ["(no metrics)"]
                c_paths = c.get("changed_paths") or []
                cand_lines.append(
                    f"    candidate {c.get('candidate')}: selected={bool(c.get('selected'))} | "
                    f"family={c.get('family','?')} | candidate_sha={c.get('sha') or '(no commit)'} | "
                    f"{' '.join(cm_parts)} | changed: {','.join(c_paths) if c_paths else '(none)'} | "
                    f"score={c.get('score')} | risk={c.get('risk','?')} | "
                    f"landing={c.get('landing_state')} | "
                    f"feedback_for_proposer=\"{c.get('feedback_for_proposer','')}\" | "
                    f'proposal="{c.get("proposal") or ""}"'
                )
            hist_lines.append(
                f"  round {r['round']}: parent_sha={r.get('parent_sha') or r.get('base_sha')} | "
                f"selected_candidate={r.get('selected_candidate')} | "
                f"selected_sha={r.get('selected_sha') or r.get('base_sha')}\n" +
                "\n".join(cand_lines)
            )
        hist_block = "\n".join(hist_lines)
    else:
        hist_block = "  (none yet — this is the first round)"


    semantic = load_semantic("proposer", prompt_dir)
    prompt = f"""{semantic}

Task goal:
{goal}

Gates:
{gate_block}

Current accepted revision:
- base_sha: {base_sha}

Accumulated search insights:
{insights_block}

Previous outcomes:
{hist_block}

Runtime context:
- Generate exactly {candidates_per_round} candidates.
- Every candidate starts from the accepted revision above.
- Editable paths: {editable}
- Frozen paths: {frozen}

Source access is read-only. Commands such as `git show {base_sha}:<path>`,
`git grep <pattern> {base_sha}`, and `simpleloop memory show <ref>` support
investigation of the accepted source and relevant historical episodes.

Fixed delivery protocol:
- Return one JSON object matching the supplied schema.
- reflection and insight are strings; insight_refs is a list of historical
  `r<round>c<candidate>` references.
- proposals contains exactly {candidates_per_round} objects with family,
  decision (`continue` or `switch`), and proposal.
- family values are distinct, nonblank labels.
- Source files remain unchanged during proposal generation.

Return JSON only, matching the supplied schema.
"""

    data = agent.run_json(
        prompt,
        cwd=cwd,
        label="proposer",
        json_schema=_proposer_schema(candidates_per_round),
    )
    batch = _parse_batch(data, candidates_per_round=candidates_per_round)
    if not batch.reflection and visible:
        # Round 0 may omit reflection; later rounds should provide the routing judgment.
        print(f"[proposer] reflection empty (history exists) — contract lapse, proceeding",
              flush=True)
    return batch


def _parse_batch(data: dict, *, candidates_per_round: int) -> ProposalBatch:
    """Validate and normalize one exact-K structured proposer response."""
    if not isinstance(data, dict):
        raise ValueError("proposer response must be an object")
    if set(data) != {"reflection", "insight", "insight_refs", "proposals"}:
        raise ValueError(
            "proposer response must contain only reflection, insight, "
            "insight_refs, and proposals"
        )

    reflection = data.get("reflection")
    if not isinstance(reflection, str):
        raise ValueError("reflection must be a string")
    reflection = normalize_free_text(
        reflection,
        limit=_REFLECTION_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
        label="proposer",
        field="reflection",
    )

    insight = data.get("insight")
    if not isinstance(insight, str):
        raise ValueError("insight must be a string")
    insight = normalize_free_text(
        insight,
        limit=_INSIGHT_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
        label="proposer",
        field="insight",
    )

    raw_insight_refs = data.get("insight_refs")
    if not isinstance(raw_insight_refs, list) or not all(
        isinstance(ref, str) for ref in raw_insight_refs
    ):
        raise ValueError("insight_refs must be a list of strings")
    insight_refs = [
        normalize_free_text(
            ref,
            limit=_INSIGHT_REF_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
            label="proposer",
            field=f"insight_refs[{i}]",
        )
        for i, ref in enumerate(raw_insight_refs)
    ]

    raw_proposals = data.get("proposals")
    if not isinstance(raw_proposals, list):
        raise ValueError("proposals must be a list")
    if len(raw_proposals) != candidates_per_round:
        raise ValueError(
            f"expected exactly {candidates_per_round} proposals, "
            f"got {len(raw_proposals)}"
        )

    proposals: list[Proposal] = []
    seen_families: set[str] = set()
    for i, item in enumerate(raw_proposals):
        if not isinstance(item, dict):
            raise ValueError(
                f"proposals[{i}]: must be an object, got {type(item).__name__}")
        if set(item) != {"family", "decision", "proposal"}:
            raise ValueError(
                f"proposals[{i}] must contain only family, decision, and proposal")

        family = item.get("family")
        if not isinstance(family, str) or not family.strip():
            raise ValueError(f"proposals[{i}].family must be a non-empty string")
        family = normalize_free_text(
            family,
            limit=_FAMILY_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
            label="proposer",
            field=f"proposals[{i}].family",
        )
        family_key = family.casefold()
        if family_key in seen_families:
            raise ValueError(f"proposals[{i}] has duplicate family: {family}")
        seen_families.add(family_key)

        decision = item.get("decision")
        if decision not in ("continue", "switch"):
            raise ValueError(
                f"proposals[{i}].decision must be continue or switch")

        proposal = item.get("proposal")
        if not isinstance(proposal, str) or not proposal.strip():
            raise ValueError(
                f"proposals[{i}].proposal must be a non-empty string")

        proposals.append(Proposal(
            proposal=normalize_free_text(
                proposal,
                limit=_PROPOSAL_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
                label="proposer",
                field=f"proposals[{i}].proposal",
            ),
            decision=decision,
            reflection=reflection,
            family=family,
        ))

    return ProposalBatch(
        reflection=reflection,
        insight=insight,
        insight_refs=insight_refs,
        proposals=proposals,
    )
