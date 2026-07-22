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
    proposals: list[Proposal]


def _proposal_history_field(record: dict) -> tuple[str, str]:
    if "proposal" in record:
        return "proposal", str(record.get("proposal") or "")
    return "proposal_head", str(record.get("proposal_head") or "")


def _proposer_schema(candidates_per_round: int) -> dict:
    """Return the strict structured-output contract for one proposer call."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reflection", "proposals"],
        "properties": {
            "reflection": {
                "type": "string",
                "maxLength": 600,
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
                            "maxLength": 64,
                            "pattern": r"\S",
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["continue", "switch"],
                        },
                        "proposal": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 800,
                            "pattern": r"\S",
                        },
                    },
                },
            },
        },
    }


def propose(agent: Agent, *, goal: str, editable: list[str], frozen: list[str],
            history: list[dict], base_sha: str, cwd: Path,
            candidates_per_round: int = 1,
            gate_block: str = "") -> ProposalBatch:
    """Return ProposalBatch for the next round."""
    # Project history through the proposer's view: this strips eval_block (the
    # judger's axis — the judger summarizes it into `feedback` for us). The
    # proposer only sees each prior round's proposal + score + tight feedback.
    visible = views.for_proposer(history)
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


    prompt = f"""You are the PROPOSER in an optimization loop.

Role:
Choose WHAT optimization hypothesis should be tested next and explain WHY it is
promising. Read code to select a direction, not to design the implementation.
The EXECUTOR owns all implementation-level decisions.

Task goal:
{goal}

Gates (reference for your proposal — each tests a specific quantity;
a proposal that would move that quantity beyond its limit should be avoided):
{gate_block}
Current accepted source:
- base_sha: {base_sha}
- Every candidate starts from this commit.
- Only the selected candidate enters the future accepted state.

You may inspect committed source with targeted `git show` or `git diff` commands.
There is no working tree (the per-run repo is a `--no-checkout` clone), so read
files via `git show {base_sha}:<path>` -- not Read/cat/grep, which see an empty
tree. Do not edit the repository or inspect other runs. The prior-round shas in
the history below ARE valid objects in this repo, so you may diff/show them
directly to check whether a mechanism was already attempted.

Prior-round outcomes:
{hist_block}

Choose the next direction:
- Read only enough code and history to identify the target, suspected waste, and
  optimization mechanism. Then stop exploring and produce the proposal.
- Use prior outcomes to decide whether to continue the current bottleneck or switch
  to a different one. Use all candidate outcomes as search memory, including failed,
  regressed, no-op, or non-selected ones.
- Treat objective delta against the direct prior accepted state as the primary
  evidence. Gate acceptance alone does not prove an optimization helped.
- When one candidate is requested, choose the best direction. When multiple are
  requested, choose meaningfully different mechanism families.
- Each candidate must be one coherent, one-round experiment.
- Do not write the implementation.

Constraints:
- candidates_per_round: {candidates_per_round}
- editable_paths: {editable}
- frozen_paths: {frozen}
- Do not propose a direction that requires modifying frozen_paths.

Return the configured JSON Schema:
- "reflection" (mandatory when prior history exists; round 0 may leave it empty):
  in at most 600 characters / 1–2 dense sentences, identify the historical evidence that matters for this
  round and judge whether the current target bottleneck or optimization hypothesis still
  has one concrete, substantively distinct next opportunity, or has stalled/exhausted its
  worthwhile headroom. This is the basis for the decision, not a recap of every round.

- "decision": encode the routing judgement made in `reflection` as exactly one token —
  `continue` — the reflection identifies a worthwhile next experiment on the same target
  bottleneck or optimization hypothesis.
  `switch`   — the reflection finds no worthwhile next experiment there, so another target
  bottleneck or optimization hypothesis should be pursued.

- "proposal" (the primary output): propose the single highest-value direction that follows
  from the decision. If `continue`, give a substantively distinct next experiment on the
  same target or hypothesis; if `switch`, move to a genuinely different target or hypothesis.
  Ground it in the current accepted base by naming the target file/function or subsystem,
  the suspected waste, the optimization mechanism to test, the expected benefit, and the
  one-round scope, in at most 800 characters. State what should be tested, not how to implement it.

- The three fields must form one chain: `reflection` justifies `decision`, and `proposal`
  must be the direct next action implied by that decision. If uncertain or blocked, still
  return the JSON object with a conservative, specific proposal.
- `family` must be non-blank, at most 64 characters, and unique after trimming and
  case-folding across this batch.
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
    if set(data) != {"reflection", "proposals"}:
        raise ValueError("proposer response must contain only reflection and proposals")

    reflection = data.get("reflection")
    if not isinstance(reflection, str):
        raise ValueError("reflection must be a string")
    reflection = reflection.strip()[:900]

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
        family = family.strip()
        if len(family) > 64:
            raise ValueError(f"proposals[{i}].family must be at most 64 characters")
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
            proposal=proposal.strip()[:1100],
            decision=decision,
            reflection=reflection,
            family=family,
        ))

    return ProposalBatch(reflection=reflection, proposals=proposals)
