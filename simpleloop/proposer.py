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
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["continue", "switch"],
                        },
                        "proposal": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 800,
                        },
                    },
                },
            },
        },
    }


def propose(agent: Agent, *, goal: str, editable: list[str], frozen: list[str],
            history: list[dict], base_sha: str, cwd: Path,
            candidates_per_round: int = 1) -> ProposalBatch:
    """Return ProposalBatch for the next round."""
    # Project history through the proposer's view: this strips eval_block (the
    # judger's axis — the judger summarizes it into `feedback` for us) and
    # feedback_for_report (human-facing detail). The proposer only sees each
    # prior round's proposal + score + tight feedback.
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
                        f"diagnostic=\"{c.get('feedback_for_report','')}\" | "
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
                    f"diagnostic=\"{r.get('feedback_for_report','')}\" | "
                    f'{proposal_label}="{proposal_text}"'
                )
        hist_block = "\n".join(hist_lines)
    else:
        hist_block = "  (none yet — this is the first round)"


    prompt = f"""You are the PROPOSER in an optimization loop. Choose candidate directions for the next round.

Roles in this loop (so you know what your input/output is and is not):
- PROPOSER (you): read the current accepted source and prior-round history, choose WHAT
  optimization hypothesis should be tested next, and explain WHY it is promising. You own
  direction selection, not implementation planning.
- EXECUTOR: takes one candidate direction, inspects the code needed to implement it, makes
  all implementation-level decisions within that direction, edits code in an isolated
  worktree, runs the gate, and commits. It must not replace the proposed optimization
  hypothesis with a different one.
- JUDGER: looks at one candidate's diff + metrics, grades the effect, tags a landing state,
  and writes objective diagnostic feedback. It does not choose the next direction.

Task goal:
{goal}

You are running in the per-run git repo — the same clone the executor commits each
round into. It has NO working tree (no checked-out files), so you inspect committed
content with git plumbing, not `cat`/`grep` of a live tree:
  - `git show {base_sha}:<path>`                    — read the current accepted source
  - `git show <candidate_sha>`                      — inspect one prior attempt's diff
  - `git diff <base_sha>..<candidate_sha> -- <path>` — targeted comparison when useful
The per-round shas in the history below ARE valid objects here (they are commits in
THIS repo) — you can diff/show them directly. Do NOT edit anything (no working tree
exists to edit); do NOT read other runs' repos.

Current accepted source:
- base_sha: {base_sha}
- Every candidate in this round starts from this exact commit.
- Only the selected candidate becomes part of the future source state.
- A non-selected candidate is not in the current accepted base, but its `landing`
  field (from the judger's tag) tells you what shape it had: `gate-rejected`
  (a real attempt the hard gate voided), `already-implemented` (executor found
  nothing to do), or `not-implemented` (landed but not selected). Inspect such a
  candidate's SHA when deciding whether to correct the attempt, continue the
  mechanism, or switch direction.

This round:
- candidates_per_round: {candidates_per_round}

Safety (hard rules):
- editable_paths (only these may be changed by the executor): {editable}
- frozen_paths (must never be touched): {frozen}

Hard rules on what you may propose (mandatory — violating these wastes a round):
- Do NOT propose a direction that requires editing files under frozen_paths — the gate will reject it and void the round.

Prior rounds (each carries candidate/base SHA, accepted state, metrics, changed_paths,
score, judger feedback/diagnostic, and the proposal):
{hist_block}

How to read the history:
- A selected candidate is part of the current accepted lineage; a non-selected
  candidate is not in the source but is still useful evidence — it shows a
  mechanism that was attempted and how it performed. Use all candidate outcomes
  as search memory, including failed, regressed, no-op, high-risk, or
  non-selected ones.

Guidance:
- Your job is to read enough of the code and prior-round history to point at a direction.
  Name WHERE the suspected waste is, WHAT makes it wasteful, and WHICH optimization
  mechanism should be tested. Do not decide HOW that mechanism should be represented or
  implemented in code. A proposal that fits in a few sentences is correct; a proposal that
  reads like a patch is a scope violation.
- Code inspection is for direction selection only. Once you can identify a plausible
  target, the wasteful mechanism, and an evidence-backed optimization hypothesis, stop
  inspecting code and produce the proposal. You do not need enough detail to implement the
  change; do not trace every downstream call site or inspect implementation details merely
  to make the proposal more complete.
- Do not produce an implementation plan: no edit sequence, pseudocode, patch outline, exact
  data representation, variable or member design, helper signatures, detailed control-flow
  rewrites, or call-site-by-call-site changes. Those decisions belong to the executor.
- Make a lightweight routing judgement from the prior-round history: decide whether the
  current optimization direction still has a concrete, evidence-backed next opportunity or
  is exhausted/stalled and should be replaced. This is a short routing step, not the main
  task and not an audit of every round — your output is the proposal, not the routing.
- When judging whether a round's change helped, treat its objective delta vs the direct
  prior accepted state as the primary evidence; comparison vs baseline describes cumulative
  progress, and `accepted=true` only means hard gates passed, so neither by itself proves
  that round's mechanism was beneficial.
- Use the history summary by default. If it is genuinely unclear whether a relevant
  mechanism or call site has already landed, you MAY inspect the most relevant prior SHA
  with `git show` or `git diff`. Git inspection is optional and targeted; do not
  systematically re-audit all prior rounds.
- With {candidates_per_round} candidate(s) requested, choose the single best next direction
  when {candidates_per_round} is 1 (serial-loop behavior), or diversify candidates across
  meaningfully different mechanism families when it is greater than 1. Do not submit
  near-duplicates.
- Each candidate proposal must be a single change the executor can build and the gate can verify in ONE round.

Reference:
- `base_sha` is authoritative for what is currently accepted. Historical
  candidate SHAs are evidence about attempts; `accepted=false` means their changes
  are not in that base, and the `landing` field carries the judger's shape tag
  (gate-rejected / already-implemented / not-implemented) so you need not infer it.

Machine-readable final delivery:
- Your response is delivered through the configured JSON Schema.
- `reflection` (mandatory when prior history exists; round 0 may leave it empty):
  at most 1–2 sentences stating only the historical evidence that affects this
  round's choice — whether the current direction still has a concrete next
  opportunity or has stalled/exhausted its headroom, and why. This is a
  judgement, not a recap of every round.
- `family`: a short mechanism label, such as `qpdf_bin_hoist`, `data_layout`, `loop_domain`, `control_flow`, or another precise label.
- `decision`: one of two tokens —
    `continue`  — deepen or extend the same target bottleneck / optimization hypothesis
                  with a substantively distinct next change.
    `switch`    — pursue a different target bottleneck / optimization hypothesis because
                  the current one lacks a worthwhile next change.
- `proposal`: each proposal is a concrete candidate direction, not an implementation plan.
  Ground it in the current accepted base: name the target file/function (or loop, subsystem,
  data path) and what repeated or wasteful mechanism should be reduced; state the optimization
  hypothesis to test; and give the expected benefit and experiment boundary. Do NOT write the
  implementation — no exact lines, variable or member names, pointer or container choices,
  helper APIs, edit steps, call-site-by-call-site rewrites, pseudocode, or verification
  commands. Those decisions belong to the executor."""
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
    reflection = reflection.strip()

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
            proposal=proposal.strip(),
            decision=decision,
            reflection=reflection,
            family=family,
        ))

    return ProposalBatch(reflection=reflection, proposals=proposals)
