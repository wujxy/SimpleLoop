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

from dataclasses import dataclass, field
from pathlib import Path

from .agent import Agent, AgentError
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

    `decision` is the forcing-function token for the hard rule: `continue`
    requires the proposal to be substantively different from a prior
    already-implemented round (the difference must be stated in `reflection`);
    `switch` is unconstrained.
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
    warnings: list[str] = field(default_factory=list)


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
                    cand_lines.append(
                        f"    candidate {c.get('candidate')}: selected={bool(c.get('selected'))} | "
                        f"family={c.get('family','?')} | candidate_sha={c.get('sha') or '(no commit)'} | "
                        f"{' '.join(cm_parts)} | changed: {','.join(c_paths) if c_paths else '(none)'} | "
                        f"score={c.get('score')} | risk={c.get('risk','?')} | "
                        f"feedback=\"{c.get('feedback','')}\" | "
                        f"diagnostic=\"{c.get('feedback_for_report','')}\" | "
                        f"proposal=\"{c.get('proposal','')}\""
                    )
                hist_lines.append(
                    f"  round {r['round']}: parent_sha={r.get('parent_sha') or round_base} | "
                    f"selected_candidate={r.get('selected_candidate')} | "
                    f"selected_sha={r.get('selected_sha') or r.get('base_sha')}\n" +
                    "\n".join(cand_lines)
                )
            else:
                hist_lines.append(
                    f"  round {r['round']}: candidate_sha={sha_str} | "
                    f"accepted={accepted_str} | base_sha={round_base} | {metrics_str} | "
                    f"changed: {paths_str} | score={r['score']} | risk={r.get('risk','?')} | "
                    f"feedback=\"{r['feedback']}\" | "
                    f"diagnostic=\"{r.get('feedback_for_report','')}\" | "
                    f"proposal=\"{r['proposal']}\""
                )
        hist_block = "\n".join(hist_lines)
    else:
        hist_block = "  (none yet — this is the first round)"

    prompt = f"""You are the PROPOSER in an optimization loop. Choose candidate directions for the next round.

Roles in this loop (so you know what your input/output is and is not):
- PROPOSER (you): read the per-run repo + prior-round history, choose candidate directions. The search decision is yours.
- EXECUTOR: takes one candidate direction, edits code in an isolated worktree, runs the gate, and commits. It does not choose or question the direction.
- JUDGER: looks at one candidate's diff + metrics, grades the effect, tags a landing state, and writes objective diagnostic feedback. It does not choose the next direction.

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
- A history row with a candidate SHA and accepted=false still represents a real implementation
  performed by the executor. It failed a hard gate and is not part of the current accepted base;
  do not mistake it for the executor making no changes. You may inspect that candidate when
  deciding whether to correct the attempt, continue the mechanism, or switch direction.

This round:
- candidates_per_round: {candidates_per_round}

Safety (hard rules):
- editable_paths (only these may be changed by the executor): {editable}
- frozen_paths (must never be touched): {frozen}

Hard rules on what you may propose (mandatory — violating these wastes a round):
- If `decision` is `continue`: your proposal MUST be for a different mechanism or different call sites than any prior round the judger tagged `LANDED_STATE: already-implemented` (or any prior round that landed the same area). State the difference in `reflection` — you can see what each prior round touched in the history above (changed paths, LANDED_STATE tag), so name the difference from what is visible there (drifting line numbers or rephrasing the same optimization is not a difference; if you cannot name a real mechanism difference, choose `switch` — that is the honest decision, not a fallback).
- If `decision` is `switch`: the above does not apply — pick a different mechanism family freely.
- Do NOT propose a direction that requires editing files under frozen_paths — the gate will reject it and void the round. If a prior round was tagged `LANDED_STATE: gate-rejected`, you may retry the direction (decision=continue is fine here), but narrow its scope/mechanism so it stays within editable_paths (the rejection reason names what was touched). gate-rejected is "wrong scope, fix the writing", not "done, switch tracks" — do not confuse it with already-implemented.

Prior rounds (each carries candidate/base SHA, accepted state, metrics, changed_paths,
score, judger feedback/diagnostic, and the proposal):
{hist_block}

How to read the history:
- A selected candidate is part of the current accepted lineage.
- A non-selected candidate is not in the current source, but it is still useful evidence:
  it shows a mechanism that was attempted and how it performed.
- Use all candidate outcomes as search memory. Do not ignore failed, regressed, no-op,
  high-risk, or non-selected candidates.
- The diagnostic feedback states what landed, what the test result was, and why the
  proposal worked, regressed, failed, or was inconclusive.

Guidance:
- Before proposing, make a lightweight routing judgement from the prior-round history:
  decide whether the current optimization direction still has a concrete,
  evidence-backed next opportunity or is exhausted/stalled and should be replaced.
  This is a short routing step, not the main task and not an audit of every round.
- When judging whether a round's change helped, treat its objective delta vs the direct
  prior accepted state as the primary evidence; comparison vs baseline describes cumulative
  progress, and `accepted=true` only means hard gates passed, so neither by itself proves
  that round's mechanism was beneficial.
- Use the history summary by default. If it is genuinely unclear whether a relevant
  mechanism or call site has already landed, you MAY inspect the most relevant prior SHA
  with `git show` or `git diff`. Git inspection is optional and targeted; do not
  systematically re-audit all prior rounds.
- Produce exactly {candidates_per_round} candidate proposal(s).
- If {candidates_per_round} is 1, behave like the serial loop and choose the single best next direction.
- If {candidates_per_round} is greater than 1, diversify candidates across meaningfully different mechanism families. Do not submit small wording variants of the same idea.
- Each candidate proposal must be a single change the executor can build and the gate can verify in ONE round.
- Size is not a virtue and not a sin: a large systematic refactor (one mechanism, many call sites) is fine and often the highest-value kind; do not shrink a high-payoff direction just to be safe. Point at the specific functions/modules/files and call sites it touches.
- But the direction must be a self-contained atomic change, not a multi-round plan. Forbidden in `proposal`: sequencing language like "first commit / then stack / step 1 of N / defer X to a later round / the deferred slice that round K sequenced". To eat a big direction over several rounds, propose only THIS round's slice (the slice itself, fully implementable+verifiable this round) and let a later round independently propose the next slice — never write the sequence into the proposal.
- When a direction has been tried for several rounds without improvement (or with regressions), `switch` is usually the right `decision`.

Reference:
- `base_sha` is authoritative for what is currently accepted. Historical candidate SHAs
  are evidence about attempts; accepted=false means their changes are not in that base.
- The `LANDED_STATE:` tag supplies the judger's change-shape hint. In particular,
  already-implemented means the executor found nothing to do; this differs from a real
  candidate commit that was rejected by a hard gate.

Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object. The preferred shape is:
  {{"reflection": "<one paragraph>", "proposals": [{{"family": "<mechanism label>", "decision": "<continue|switch>", "proposal": "<candidate direction>"}}]}}
- When candidates_per_round is 1, this legacy shape is also accepted:
  {{"reflection": "<one paragraph>", "decision": "<continue|switch>", "proposal": "<your direction>"}}
- A ```json code fence is acceptable; any prose, heading, commentary, or natural-language wrap-up outside the JSON is forbidden. Your INVESTIGATION (reading code, diffing rounds) goes through tool calls (git show/diff/log); your reasoning CONCLUSIONS go inside the JSON — in `reflection` and `proposal`. Emit ONLY the JSON object (no preamble, no wrap-up around it).
- `reflection` (mandatory when prior history exists; round 0 may leave it empty):
  at most 1–2 sentences stating only the historical evidence that affects this round's
  choice — whether the current direction still has a concrete next opportunity or has
  stalled/exhausted its headroom, and why. This is a judgement, not a recap of every round.
- `family`: a short mechanism label, such as `qpdf_bin_hoist`, `data_layout`, `loop_domain`, `control_flow`, or another precise label.
- `decision`: one of two tokens —
    `continue`  — deepen or extend the same target bottleneck / optimization hypothesis
                  with a substantively distinct next change.
    `switch`    — pursue a different target bottleneck / optimization hypothesis because
                  the current one lacks a worthwhile next change.
- `proposal`: each proposal is a concrete candidate direction. Ground it in the current
  accepted base, and identify the target code, mechanism, relevant call sites, expected
  benefit, bit-faithful constraints, and one-round implementation scope.
- If you are uncertain or blocked, still return the JSON object with a conservative, specific proposal.
- Do not ask for more data and do not emit a summary.

Example of the ONLY acceptable final output shape:
{{"reflection": "r4 landed NPE-map inline+hoist (not-implemented, -24%); r5-8 re-tried it (already-implemented, empty). The QPDF inline is a different mechanism family not yet landed.", "proposals": [{{"family": "qpdf_bin_hoist", "decision": "switch", "proposal": "In Calculate_EVLikelihood's k-loop (OMILRECV2.cc:1257), inline the QPDF charge-PDF interpolation kernel at the 2 call sites, hoisting the PMT_Hit-constant bin search out of the k-loop."}}]}}"""
    try:
        data = agent.run_json(prompt, cwd=cwd, label="proposer")
    except AgentError as exc:
        # The agent returned prose instead of JSON. A rough direction in prose is
        # still a usable proposal — degrade gracefully instead of wasting a round.
        # reflection/decision are lost in this path; that's acceptable (fallback is
        # degrade-gracefully), but we log it so the loss is visible, not silent.
        fallback = _prose_fallback(exc.raw_output)
        if not fallback:
            raise
        print(f"[proposer] JSON parse failed; using prose fallback as proposal "
              f"(reflection/decision lost)", flush=True)
        return ProposalBatch(reflection="", proposals=[Proposal(proposal=fallback)],
                             warnings=["JSON parse failed; prose fallback produced one candidate"])
    batch = _parse_batch(data, candidates_per_round=candidates_per_round)
    for warning in batch.warnings:
        print(f"[proposer] {warning}", flush=True)
    if not batch.reflection and visible:
        # round 0 is allowed to skip reflection (no history to reflect on); later
        # rounds omitting it is a contract lapse but not worth failing a round over.
        print(f"[proposer] reflection empty (history exists) — contract lapse, proceeding",
              flush=True)
    return batch


def _parse_batch(data: dict, *, candidates_per_round: int) -> ProposalBatch:
    """Normalize new batch JSON and legacy single-proposal JSON."""
    reflection = str(data.get("reflection", "")).strip()
    warnings: list[str] = []
    raw_proposals = data.get("proposals")
    proposals: list[Proposal] = []
    if isinstance(raw_proposals, list):
        for i, item in enumerate(raw_proposals):
            if not isinstance(item, dict):
                raise ValueError(f"proposals[{i}]: must be an object, got {type(item).__name__}")
            proposal = item.get("proposal")
            if not isinstance(proposal, str) or not proposal.strip():
                raise ValueError(f"proposals[{i}].proposal: must be a non-empty string")
            decision = _normalize_decision(item.get("decision", "switch"))
            family = str(item.get("family") or f"candidate_{i}").strip() or f"candidate_{i}"
            proposals.append(Proposal(proposal=proposal.strip(), decision=decision,
                                      reflection=reflection, family=family))
    elif isinstance(data.get("proposal"), str) and data.get("proposal", "").strip():
        if candidates_per_round > 1:
            warnings.append(
                f"returned legacy single-proposal JSON for candidates_per_round="
                f"{candidates_per_round}; degrading to one candidate"
            )
        proposals.append(Proposal(proposal=data["proposal"].strip(),
                                  decision=_normalize_decision(data.get("decision", "switch")),
                                  reflection=reflection,
                                  family=str(data.get("family") or "single").strip() or "single"))
    else:
        raise ValueError(f"proposer did not return proposals[] or a non-empty proposal: {data}")

    if not proposals:
        raise ValueError("proposer returned an empty proposals list")
    if candidates_per_round == 1 and len(proposals) > 1:
        warnings.append("returned multiple proposals for candidates_per_round=1; using the first")
        proposals = proposals[:1]
    if candidates_per_round > 1 and len(proposals) != candidates_per_round:
        warnings.append(
            f"returned {len(proposals)} candidate(s), expected {candidates_per_round}; "
            "running the returned candidates"
        )
    return ProposalBatch(reflection=reflection, proposals=proposals, warnings=warnings)


def _normalize_decision(value: object) -> str:
    decision = str(value or "switch").strip().lower()
    if decision not in ("continue", "switch"):
        print(f"[proposer] decision '{decision}' not continue|switch; defaulting to switch",
              flush=True)
        return "switch"
    return decision


def _prose_fallback(raw: str) -> str | None:
    """If the agent emitted prose instead of JSON, salvage it as the proposal.

    Returns the non-empty prose (trimmed) or None if there's nothing usable. We
    don't try to parse a JSON object here — run_json already tried and failed.
    """
    if not raw:
        return None
    # drop a leading ```json fence if the agent half-fenced prose
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    return text or None
