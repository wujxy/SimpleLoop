"""Proposer: reads the per-run repo (read-only) + history -> proposes one direction.

Sees: task goal, safety (editable/frozen), the authoritative accepted base SHA,
and round history (proposal/candidate sha/accepted/base sha/score/metrics/
changed_paths/feedback for each prior round). Its cwd is the
PER-RUN working repo — the same clone the executor commits each round into —
so every prior round's sha is a valid object it can `git show`/`git diff` to
self-audit whether a direction was attempted and accepted (see the diff that
round actually produced) or read the current accepted state of any file.
It must NOT edit any file (the executor does that, in a per-round worktree)
and must NOT read other runs' repos (cross-run answer-copyting); the per-run
clone is physically isolated per run_dir.

The per-run repo has NO working tree (cloned with --no-checkout), so the
proposer reads committed content via git plumbing, not `cat`/`grep` of a live
tree: `git show <sha>:<path>`, `git diff <a>..<b> -- <path>`, `git log`.

Delivers: a Proposal(proposal=..., decision=..., reflection=...). `proposal` is the
direction string the executor implements; `decision` (continue|switch) and
`reflection` are stored in history.jsonl as human-audit evidence but NOT fed
back into the next round's prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from . import views
from . import memory as memory_mod


_STRUCTURED_TEXT_MARGIN = 300
_REFLECTION_GENERATION_LIMIT = 600
_INSIGHT_GENERATION_LIMIT = 500
_INSIGHT_REF_GENERATION_LIMIT = 32
_FAMILY_GENERATION_LIMIT = 64
_PROPOSAL_GENERATION_LIMIT = 800


@dataclass
class Proposal:
    """What the proposer returns each round.

    `proposal` is the only field the executor consumes — it stays a plain
    direction string, so the executor is untouched. `reflection` and
    `decision` are stored in history.jsonl as human-audit evidence (so you can
    later see what the proposer reflected before re-proposing a direction),
    but they are NOT fed back into the next round's prompt — `views.for_proposer`
    does not project them — so they never bias the next round's decision.

    """
    proposal: str
    decision: str = "switch"   # default switch (conservative) if the judger omitted it
    reflection: str = ""
    family: str = "single"


@dataclass
class ProposalBatch:
    """One round's candidate directions.

    `reflection` is shared search-context reasoning for the round. Each
    candidate has its own family/decision/proposal triple.
    """
    reflection: str
    insight: str
    insight_refs: list[str]
    proposals: list[Proposal]


def _proposal_history_field(record: dict) -> tuple[str, str]:
    if "proposal" in record:
        return "proposal", str(record.get("proposal") or "")
    return "proposal_head", str(record.get("proposal_head") or "")


def _proposer_schema(candidates_per_round: int) -> dict:
    """Accept exact structure while leaving N+300 headroom for free text."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reflection", "insight", "insight_refs", "proposals"],
        "properties": {
            "reflection": {
                "type": "string",
                "maxLength": _REFLECTION_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
            },
            "insight": {
                "type": "string",
                "maxLength": _INSIGHT_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
            },
            "insight_refs": {
                "type": "array",
                "items": {
                    "type": "string",
                    "maxLength": _INSIGHT_REF_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
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
                            "maxLength": _FAMILY_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
                            "pattern": r"\S",
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["continue", "switch"],
                        },
                        "proposal": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _PROPOSAL_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
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
            gate_block: str = "") -> ProposalBatch:
    """Return ProposalBatch for the next round."""
    # Project history through the proposer's view: this strips eval_block (the
    # judger's axis — the judger summarizes it into `feedback` for us). The
    # proposer only sees each prior round's proposal + score + tight feedback.
    visible = views.for_proposer(history)
    insights_block = memory_mod.render_insights(insights)
    if visible:
        hist_lines = []
        for r in visible:
            sha = r.get("sha")
            sha_str = sha if sha else "(no commit)"
            accepted = r.get("accepted")
            accepted_str = "unknown" if accepted is None else str(bool(accepted)).lower()
            round_base = r.get("base_sha") or "(legacy/unknown)"
            m = r.get("metrics") or {}
            m_parts = [f"{k}={v}" for k, v in m.items()] if m else ["(no metrics)"]
            metrics_str = " ".join(m_parts)
            paths = r.get("changed_paths") or []
            paths_str = ",".join(paths) if paths else "(none)"
            if "candidates" in r:
                cand_lines = []
                for c in r.get("candidates") or []:
                    cm = c.get("metrics") or {}
                    cm_parts = [f"{k}={v}" for k, v in cm.items()] if cm else ["(no metrics)"]
                    c_paths = c.get("changed_paths") or []
                    proposal_label, proposal_text = _proposal_history_field(c)
                    cand_lines.append(
                        f"    candidate {c.get('candidate')}: selected={bool(c.get('selected'))} | "
                        f"family={c.get('family','?')} | candidate_sha={c.get('sha') or '(no commit)'} | "
                        f"{' '.join(cm_parts)} | changed: {','.join(c_paths) if c_paths else '(none)'} | "
                        f"score={c.get('score')} | risk={c.get('risk','?')} | "
                        f"landing={c.get('landing_state')} | "
                        f"feedback=\"{c.get('feedback','')}\" | "
                        f'{proposal_label}="{proposal_text}"'
                    )
                hist_lines.append(
                    f"  round {r['round']}: parent_sha={r.get('parent_sha') or round_base} | "
                    f"selected_candidate={r.get('selected_candidate')} | "
                    f"selected_sha={r.get('selected_sha') or r.get('base_sha')}\n" +
                    "\n".join(cand_lines)
                )
            else:
                proposal_label, proposal_text = _proposal_history_field(r)
                landing = r.get("landing_state")
                hist_lines.append(
                    f"  round {r['round']}: candidate_sha={sha_str} | "
                    f"accepted={accepted_str} | base_sha={round_base} | {metrics_str} | "
                    f"changed: {paths_str} | score={r['score']} | risk={r.get('risk','?')} | "
                    f"landing={landing} | "
                    f"feedback=\"{r['feedback']}\" | "
                    f'{proposal_label}="{proposal_text}"'
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
    reflection = reflection.strip()[
        :_REFLECTION_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN
    ]

    insight = data.get("insight")
    if not isinstance(insight, str):
        raise ValueError("insight must be a string")
    insight = insight.strip()[
        :_INSIGHT_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN
    ]

    raw_insight_refs = data.get("insight_refs")
    if not isinstance(raw_insight_refs, list) or not all(
        isinstance(ref, str) for ref in raw_insight_refs
    ):
        raise ValueError("insight_refs must be a list of strings")
    insight_refs = [
        ref.strip()[:_INSIGHT_REF_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN]
        for ref in raw_insight_refs
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
        family = family.strip()[
            :_FAMILY_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN
        ]
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
            proposal=proposal.strip()[
                :_PROPOSAL_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN
            ],
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
