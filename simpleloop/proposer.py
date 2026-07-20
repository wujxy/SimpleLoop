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


def propose(agent: Agent, *, goal: str, editable: list[str], frozen: list[str],
            history: list[dict], base_sha: str, cwd: Path) -> Proposal:
    """Return the Proposal(proposal, decision, reflection) for the next round."""
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
            hist_lines.append(
                f"  round {r['round']}: candidate_sha={sha_str} | "
                f"accepted={accepted_str} | base_sha={round_base} | {metrics_str} | "
                f"changed: {paths_str} | score={r['score']} | risk={r.get('risk','?')} | "
                f"feedback=\"{r['feedback']}\" | proposal=\"{r['proposal']}\""
            )
        hist_block = "\n".join(hist_lines)
    else:
        hist_block = "  (none yet — this is the first round)"

    prompt = f"""You are the PROPOSER in a serial optimization loop. Propose the next round's direction.

Roles in this loop (so you know what your input/output is and is not):
- PROPOSER (you): read the per-run repo + prior-round history, choose the next direction. The direction decision is yours.
- EXECUTOR: takes your direction, edits code (in a per-round worktree), runs the gate, commits. It does not choose or question the direction.
- JUDGER: looks at ONE round's diff + metrics, grades the effect, tags a landing state. It sees a single round (not the whole direction space) and does not profile — so its feedback is a reference for you, not a direction command.

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
- This is the exact commit from which the next executor attempt will start.
- A history row with a candidate SHA and accepted=false still represents a real implementation
  performed by the executor. It failed a hard gate and is not part of the current accepted base;
  do not mistake it for the executor making no changes. You may inspect that candidate when
  deciding whether to correct the attempt, continue the mechanism, or switch direction.

Safety (hard rules):
- editable_paths (only these may be changed by the executor): {editable}
- frozen_paths (must never be touched): {frozen}

Hard rules on what you may propose (mandatory — violating these wastes a round):
- If `decision` is `continue`: your proposal MUST be for a different mechanism or different call sites than any prior round the judger tagged `LANDED_STATE: already-implemented` (or any prior round that landed the same area). State the difference in `reflection` — you can see what each prior round touched in the history above (changed paths, LANDED_STATE tag), so name the difference from what is visible there (drifting line numbers or rephrasing the same optimization is not a difference; if you cannot name a real mechanism difference, choose `switch` — that is the honest decision, not a fallback).
- If `decision` is `switch`: the above does not apply — pick a different mechanism family freely.
- Do NOT propose a direction that requires editing files under frozen_paths — the gate will reject it and void the round. If a prior round was tagged `LANDED_STATE: gate-rejected`, you may retry the direction (decision=continue is fine here), but narrow its scope/mechanism so it stays within editable_paths (the rejection reason names what was touched). gate-rejected is "wrong scope, fix the writing", not "done, switch tracks" — do not confuse it with already-implemented.

Prior rounds (each carries candidate/base SHA, accepted state, metrics, changed_paths,
score, judger feedback, and the proposal):
{hist_block}

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
- Propose exactly one direction for the next round — a single change the executor can build and the gate can verify in ONE round.
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
- Your final response MUST be exactly one parseable JSON object with THREE keys:
  {{"reflection": "<one paragraph>", "decision": "<continue|switch>", "proposal": "<your direction>"}}
- These three form a lightweight routing chain, not three equal tasks: `reflection` and
  `decision` briefly select the search path; `proposal` is the main task.
- A ```json code fence is acceptable; any prose, heading, commentary, or natural-language wrap-up outside the JSON is forbidden. Your INVESTIGATION (reading code, diffing rounds) goes through tool calls (git show/diff/log); your reasoning CONCLUSIONS go inside the JSON — in `reflection` and `proposal`. Emit ONLY the JSON object (no preamble, no wrap-up around it).
- `reflection` (mandatory when prior history exists; round 0 may leave it empty):
  at most 1–2 sentences stating only the historical evidence that affects this round's
  choice — whether the current direction still has a concrete next opportunity or has
  stalled/exhausted its headroom, and why. This is a judgement, not a recap of every round.
- `decision`: one of two tokens —
    `continue`  — deepen or extend the same target bottleneck / optimization hypothesis
                  with a substantively distinct next change.
    `switch`    — pursue a different target bottleneck / optimization hypothesis because
                  the current one lacks a worthwhile next change.
- `proposal` (the primary output): propose the single highest-value concrete change that
  follows from the decision. Spend most of your investigation and reasoning here. Ground it
  in the current accepted base, and identify the target code, mechanism, relevant call sites, expected
  benefit, and one-round implementation scope. Subject to the hard rules above.
- If you are uncertain or blocked, still return the JSON object with a conservative, specific proposal.
- Do not ask for more data and do not emit a summary.

Example of the ONLY acceptable final output shape:
{{"reflection": "r4 landed NPE-map inline+hoist (not-implemented, -24%); r5-8 re-tried it (already-implemented, empty). The QPDF inline is a different mechanism family not yet landed.", "decision": "switch", "proposal": "In Calculate_EVLikelihood's k-loop (OMILRECV2.cc:1257), inline the QPDF charge-PDF interpolation kernel at the 2 call sites, hoisting the PMT_Hit-constant bin search out of the k-loop."}}"""
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
        return Proposal(proposal=fallback)
    proposal = data.get("proposal")
    if not isinstance(proposal, str) or not proposal.strip():
        raise ValueError(f"proposer did not return a non-empty 'proposal' string: {data}")
    decision = str(data.get("decision", "switch")).strip().lower()
    # the contract says continue|switch; accept anything but normalize unknown to
    # switch (the conservative default — switch is unconstrained, continue requires
    # the reflection to state a mechanism difference, which we can't enforce here).
    if decision not in ("continue", "switch"):
        print(f"[proposer] decision '{decision}' not continue|switch; defaulting to "
              f"switch", flush=True)
        decision = "switch"
    reflection = str(data.get("reflection", "")).strip()
    if not reflection and visible:
        # round 0 is allowed to skip reflection (no history to reflect on); later
        # rounds omitting it is a contract lapse but not worth failing a round over.
        print(f"[proposer] reflection empty (history exists) — contract lapse, proceeding",
              flush=True)
    return Proposal(proposal=proposal.strip(), decision=decision, reflection=reflection)


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
