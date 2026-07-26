"""Proposer: reads the per-run repo (read-only) + history and proposes the next
candidate directions. It never edits files; the executor implements in a worktree."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent import Agent, normalize_free_text
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
            gate_block: str = "") -> ProposalBatch:
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


    prompt = f"""You are the PROPOSER in an iterative optimization search.

Your job is to propose the next {candidates_per_round} optimization experiments:
decide what may be worth trying, based on the accepted source and what previous
experiments actually taught us.

You are one part of a team:
- You propose optimization hypotheses and search directions.
- The EXECUTOR investigates implementation details and implements each proposal.
- The JUDGER evaluates the resulting diff, correctness, performance, and risk.
- The harness uses measured results and gates to decide what enters the accepted source.

Use previous outcomes as accumulated search experience. Successful experiments
may reveal promising mechanisms or code regions. Failed, regressed, rejected,
and nonselected experiments are also useful: learn from them instead of simply
repeating them. Pay particular attention to the objective change relative to
the direct accepted parent. Passing a gate means an experiment was valid; it
does not by itself mean the idea was beneficial.

Use the current accepted source to keep proposals connected to real code.
Source reading supports proposal generation; it is not a separate code-review
or verification task. You do not need to trace every call, inspect every helper,
prove invariants, or design the implementation. Treat recorded history as
trustworthy rather than re-auditing old revisions. Once an idea refers to real
accepted code and has a plausible optimization mechanism, it is grounded enough
to propose.

A proposal is a grounded hypothesis, not an implementation conclusion. It is
allowed to be uncertain and it is allowed to fail.

Task goal:
{goal}

Gates:
{gate_block}

Keep the gates in mind when choosing proposals, but do not try to prove that a
future implementation will pass them. Avoid obvious conflicts; implementation
and validation belong to the EXECUTOR and JUDGER.

Current accepted revision:
- base_sha: {base_sha}

Accumulated search insights:
{insights_block}

Every candidate in this batch starts from the same accepted revision. A
candidate affects later generations only if the harness selects and accepts it.

Previous outcomes:
{hist_block}

Propose exactly {candidates_per_round} experiments. They should explore
meaningfully different ideas rather than minor variants of the same change.
Each should be coherent enough to attempt as one round of work.

A useful proposal gives the EXECUTOR enough direction to begin: identify a real
code area, a plausible source of waste, the broad optimization mechanism, why
it may help, and a reasonable one-round scope. Leave concrete data structures,
APIs, cache lifetimes, call rewiring, and other implementation choices to the
EXECUTOR. Do not turn the proposal into an implementation plan or a guarantee.

The EXECUTOR needs a grounded direction, not a finished investigation. Once the
EXECUTOR has enough to take over, return the proposals rather than continuing
to improve the completeness of your investigation.

Source access:
- You do not have an editable worktree.
- When a small amount of source context would help, inspect the accepted
  revision with commands such as `git show {base_sha}:<path>` or
  `git grep <pattern> {base_sha}`.
- When an important accumulated insight is too compact to support the choice,
  you may inspect one supporting episode with `simpleloop memory show <ref>`.
- Do not edit files, switch revisions, or run the optimization task yourself.

Safety boundaries:
- Editable paths: {editable}
- Frozen paths: {frozen}
- Do not propose a direction that requires modifying frozen paths.

The output fields form one connected reasoning chain:

previous evidence -> reflection -> optional insight -> decision -> proposal

`reflection`:
- At most 600 characters.
- Give the batch-level search rationale in at most 1–2 dense sentences: use the
  accumulated insights, the most relevant recent outcomes, and the current
  accepted source to identify the historical evidence that matters now.
- Judge whether the current target bottlenecks or optimization hypotheses still
  have concrete, substantively distinct opportunities, or have stalled or
  exhausted their worthwhile headroom.
- It is not a single continue/switch verdict for the whole batch and does not
  need to prove every candidate individually.
- Accumulated insights are compact guides to older experience. When an important
  insight is too compact, conflicts with recent evidence, may no longer match
  the current source, or supports revisiting an old direction, you may inspect
  its referenced episode before deciding.
- Avoid merely recapping history. It may be empty only when there is no previous
  outcome to learn from.

`insight`:
- If the reflection yields a durable lesson not already captured by the
  accumulated insights and useful to future rounds, give its smallest reusable
  form as one or two concise, generalizing sentences.
- The insight is the durable part of the reflection, not a separate recap,
  implementation note, or proposal. Otherwise return an empty string.

`insight_refs`:
- Give the exact historical candidate references supporting the new insight,
  using `r<round>c<candidate>`, for example ["r0c0", "r1c1"].
- Return an empty list when `insight` is empty.

`decision`:
- For each proposal, use `continue` when it develops a promising area or
  mechanism supported by the reflection.
- Use `switch` when it moves to a different direction in light of that
  reflection.

For each proposal:
- `family`: a nonblank label of at most 64 characters. Family labels must be
  unique after trimming whitespace and ignoring case.
- `proposal`: a nonblank grounded hypothesis of at most 800 characters that
  follows from its decision and makes sense under the batch rationale. Name
  the target area, suspected waste, broad mechanism, expected benefit, and
  one-round scope. Describe what may be worth trying, not exactly how to code it.

The fields should stay connected: `reflection` explains the current
understanding, `insight` preserves only the reusable part when one exists, each
`decision` expresses the resulting search judgement, and each `proposal` is the
next action implied by that judgement.

If evidence is limited, prefer a conservative, source-grounded hypothesis. Do
not keep investigating merely to turn uncertainty into certainty.

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
