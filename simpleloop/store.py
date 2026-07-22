"""History store: append-only JSONL of each round + best tracking.

Each round records {round, proposal, sha, accepted, base_sha, score, risk,
feedback, feedback_for_report, eval_block, metrics}. `sha` is the attempted
candidate; `base_sha` is the accepted cumulative source after that round. The
best commit is selected by the HARNESS, by the real objective metric — NOT by
the judger's subjective 0-1 score. This is the fix for
the "best by score" problem surfaced in the 12-round OMILRECV2 run, where the
highest-score round (r5, 0.88, 571ms) locked out the fastest correct commit
(r10, 0.85, 321ms): once the harness owns the real speed (parsed from eval
output), best selection becomes "among gate-pass + risk-not-high rounds, take
the best objective" — score demotes to a quality signal, not a ranking number.

Best selection rule (only when metrics_schema is configured):
  eligible = rounds where every declared gate metric is True (PASS) AND
             judger-risk != "high" AND the objective metric is present+numeric.
  best = the eligible round with the best objective (min if lower_is_better,
         else max). First such round wins ties.
A failed-gate or high-risk round is never best, even if its objective is best —
this is what keeps a latent-risk flag (e.g. r5's MODE-keyed cache validity) from
being the chosen ship commit when a safer later round is nearly as fast.

Without a metrics_schema (diff-only / legacy configs), best falls back to the
old score-based rule so nothing breaks.

eval_block is the raw harness-run eval output for the round (capped), stored so
the loop can feed the prior round's eval + the baseline eval to the next round's
judger as explicit comparison axes. metrics is the harness-parsed key=value dict
(the authoritative numbers); the raw text is kept for the record and for the
judger to verify a specific claim, but the parsed metrics are what the judger
cites and what best selection uses.

No separate event/artifact/execution stores — one JSONL covers everything.
"""
from __future__ import annotations

import json
from pathlib import Path

# Cap the eval_block stored per round. The live eval_block fed to the judger is
# capped separately (run_eval's _OUT_CAP); this cap just keeps history.jsonl from
# ballooning when eval is verbose. 6000 chars is plenty for sl_eval's ~5KB output
# plus headroom, and the judger only needs the result lines (CORRECTNESS=/SPEED_MS=).
_HIST_EVAL_CAP = 6000


class Store:
    def __init__(self, run_dir: Path, metrics_schema: dict | None = None):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "history.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # metrics_schema: {"objective": {key, lower_is_better}, "gates": [{key}]}
        # or None (diff-only / legacy -> best by score). Set once at loop start;
        # drives best selection.
        self.metrics_schema = metrics_schema
        self.best_score: float = -1.0           # judger quality number (report only)
        self.best_sha: str | None = None        # harness-selected best commit
        self.best_round: int | None = None
        self.best_candidate: int | None = None
        # For score-based fallback / report divergence:
        self.best_by_score_sha: str | None = None
        self.best_by_score_round: int | None = None

    def append(self, round_id: int, proposal: str, sha: str | None,
               score: float | None, feedback: str,
               eval_block: str = "",
               eval_metrics: dict | None = None,
               risk: str = "high",
               changed_paths: list[str] | None = None,
               reflection: str = "",
               decision: str = "",
               accepted: bool = False,
               base_sha: str | None = None) -> None:
        """Record one round and update best selection.

        risk defaults to 'high' — a caller that doesn't supply one (e.g. a loop
        failure) is treated as not-best-eligible, which is the safe default for a
        round where we couldn't even get a risk read.

        changed_paths: the files the executor touched this round (already computed
        by workspace.changed_paths for the gate). Stored so the proposer can see
        what each prior round changed without running git — a cheap landing-state
        signal that complements sha (the proposer can `git diff` the sha for
        detail). Empty list on gate-rejected / no-change / failed rounds.

        reflection + decision: the proposer's reflection paragraph and continue|
        switch token. Stored as HUMAN-AUDIT EVIDENCE (so you can later see what
        the proposer reflected before re-proposing a direction) but NOT projected
        back into the next round's prompt (views.for_proposer omits them), so a
        prior decision never biases the next round's choice.

        sha remains the candidate commit for backward compatibility. accepted
        says whether all configured hard gates passed; base_sha is the accepted
        source after this round and therefore the next executor's starting point.
        """
        record = {
            "round": round_id,
            "proposal": proposal,
            "sha": sha,
            "score": score,
            "risk": risk,
            "feedback": feedback,
            "eval_block": (eval_block or "")[:_HIST_EVAL_CAP],
            "metrics": eval_metrics or {},
            "changed_paths": changed_paths or [],
            "reflection": reflection,
            "decision": decision,
            "accepted": bool(accepted),
            "base_sha": base_sha,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._recompute_best()

    def history(self) -> list[dict]:
        """Read all rounds back (for the proposer's prompt)."""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def append_generation(self, round_id: int, *, parent_sha: str,
                          selected_candidate: int | None,
                          selected_sha: str | None,
                          candidates: list[dict],
                          reflection: str = "") -> None:
        """Record a self-loop generation with multiple candidate attempts."""
        normalized = []
        for i, c in enumerate(candidates):
            normalized.append({
                "candidate": c.get("candidate", i),
                "family": c.get("family") or "single",
                "proposal": c.get("proposal") or "",
                "sha": c.get("sha"),
                "score": c.get("score"),
                "risk": c.get("risk", "high"),
                "decision": c.get("decision", ""),
                "feedback": c.get("feedback", ""),
                "eval_block": (c.get("eval_block") or "")[:_HIST_EVAL_CAP],
                "metrics": c.get("metrics") or {},
                "changed_paths": c.get("changed_paths") or [],
                "accepted": bool(c.get("accepted")),
                "selected": c.get("candidate", i) == selected_candidate,
            })
        selected = next((c for c in normalized if c["selected"]), None)
        record = {
            "round": round_id,
            "parent_sha": parent_sha,
            "selected_candidate": selected_candidate,
            "selected_sha": selected_sha,
            "sha": selected_sha,
            "proposal": selected.get("proposal", "") if selected else "",
            "score": selected.get("score") if selected else 0.0,
            "risk": selected.get("risk", "high") if selected else "high",
            "feedback": selected.get("feedback", "") if selected else "[no selected candidate]",
            "metrics": selected.get("metrics", {}) if selected else {},
            "changed_paths": selected.get("changed_paths", []) if selected else [],
            "accepted": bool(selected_sha),
            "base_sha": selected_sha or parent_sha,
            "reflection": reflection,
            "decision": selected.get("decision", "") if selected else "",
            "candidates": normalized,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._recompute_best()

    def last_eval_block(self) -> str | None:
        """The most recent round's eval_block, or None if no rounds yet.

        The loop uses this as the 'prior round' comparison axis for the next
        round's judger (round 0's prior falls back to the baseline eval).
        """
        rounds = self.history()
        if not rounds:
            return None
        return rounds[-1].get("eval_block") or None

    def _recompute_best(self) -> None:
        """Recompute best over the full history.

        Metric-based when a schema is set; score-based fallback otherwise. Done
        from scratch each append (cheap for tens-of-rounds histories) so that
        adding a schema or changing the rule never leaves stale best state.
        """
        rounds = self.history()
        if not rounds:
            return
        # always track the highest-score round (for the report's divergence line)
        candidates = list(_iter_candidates(rounds))
        scored = [c for c in candidates
                  if c.get("sha") and isinstance(c.get("score"), (int, float))]
        if scored:
            top = max(scored, key=lambda c: c["score"])
            self.best_by_score_sha = top["sha"]
            self.best_by_score_round = top["round"]
            # score-based fallback (and the legacy public field)
            if self.metrics_schema is None:
                self.best_sha = top["sha"]
                self.best_round = top["round"]
                self.best_candidate = top.get("candidate")
                self.best_score = top["score"]

        if self.metrics_schema is None:
            return  # score-based best already set above

        obj = self.metrics_schema["objective"]
        obj_key = obj["key"]
        lower = obj["lower_is_better"]
        gate_keys = [g["key"] for g in self.metrics_schema.get("gates", [])]

        best = None  # (round, sha, obj_value)
        for r in candidates:
            sha = r.get("sha")
            if not sha:
                continue
            m = r.get("metrics") or {}
            # gate-pass: every declared gate must be True (present + PASS).
            if not all(m.get(gk) is True for gk in gate_keys):
                continue
            # risk not high
            if str(r.get("risk", "high")).lower() == "high":
                continue
            # objective present + numeric
            ov = m.get(obj_key)
            if not isinstance(ov, (int, float)):
                continue
            if best is None:
                best = (r["round"], sha, ov, r.get("candidate"), r.get("score") or -1.0)
                continue
            better = (ov < best[2]) if lower else (ov > best[2])
            tied = ov == best[2]
            if tied:
                better = (r.get("score") or -1.0) > best[4]
            if better:
                best = (r["round"], sha, ov, r.get("candidate"), r.get("score") or -1.0)
        if best is not None:
            self.best_sha = best[1]
            self.best_round = best[0]
            self.best_candidate = best[3]
            # keep best_score as the judger score of that round for the report
            rec = next((r for r in candidates
                        if r["round"] == best[0] and r.get("sha") == best[1]), None)
            if rec and isinstance(rec.get("score"), (int, float)):
                self.best_score = rec["score"]

    def write_final_report(self, goal: str) -> Path:
        """Write a human-readable markdown summary. Returns its path."""
        rounds = self.history()
        lines = [
            "# SimpleLoop Run Report",
            "",
            f"**Goal:** {goal}",
            "",
            "![Run progress](progress.png)",
            "",
        ]
        if self.best_sha:
            cand = (f", candidate {self.best_candidate}"
                    if self.best_candidate is not None else "")
            lines += [f"- Best commit: `{self.best_sha}` (round {self.best_round}{cand}, "
                      f"selected by {'objective metric' if self.metrics_schema else 'judger score'}"
                      f", score {self.best_score:.2f})", ""]
        else:
            lines += ["- No accepted commit produced.", ""]
        # Report divergence between metric-best and score-best when they differ —
        # this is the exact tension the harness-owned best was built to surface.
        if (self.metrics_schema and self.best_by_score_sha
                and self.best_by_score_sha != self.best_sha):
            lines += [f"- Highest-score round (judger score): `{self.best_by_score_sha}` "
                      f"(round {self.best_by_score_round}) — differs from the metric-selected best; "
                      f"the metric best is what ships, the score-best is a quality signal.",
                      ""]
        lines += ["## Round History", ""]
        for r in rounds:
            if "candidates" in r:
                lines += [f"### Round {r['round']}",
                          f"- parent sha: `{r.get('parent_sha')}`",
                          f"- selected candidate: {r.get('selected_candidate')}  "
                          f"selected sha: `{r.get('selected_sha')}`",
                          f"- reflection: {r.get('reflection', '')}", "",
                          "| candidate | selected | family | sha | metrics | score | risk | feedback |",
                          "|---|---|---|---|---|---|---|---|"]
                for c in r.get("candidates") or []:
                    m = c.get("metrics") or {}
                    metrics_text = " ".join(f"{k}={v}" for k, v in m.items()) if m else ""
                    lines.append(
                        f"| {c.get('candidate')} | {bool(c.get('selected'))} | "
                        f"{c.get('family', '')} | `{c.get('sha')}` | {metrics_text} | "
                        f"{c.get('score')} | {c.get('risk')} | {c.get('feedback', '')[:180]} |"
                    )
                for c in r.get("candidates") or []:
                    lines += ["",
                              f"#### Round {r['round']} candidate {c.get('candidate')}",
                              f"- proposal: {c.get('proposal', '')[:300]}",
                              f"- changed paths: {', '.join(c.get('changed_paths') or []) or '(none)'}",
                              f"- feedback: {c.get('feedback', '')}"]
                lines.append("")
                continue
            m = r.get("metrics") or {}
            metrics_line = ""
            if self.metrics_schema and m:
                obj_key = self.metrics_schema["objective"]["key"]
                parts = []
                if obj_key in m:
                    parts.append(f"{obj_key}={m[obj_key]}")
                for g in self.metrics_schema.get("gates", []):
                    if g["key"] in m:
                        v = m[g["key"]]
                        parts.append(f"{g['key']}={'PASS' if v is True else 'FAIL' if v is False else '?'}")
                if parts:
                    metrics_line = f"  metrics: {' '.join(parts)}"
            lines += [f"### Round {r['round']}",
                      f"- proposal: {r['proposal'][:200]}",
                      f"- candidate sha: `{r['sha']}`  accepted: "
                      f"{r.get('accepted', '?')}  base sha: `{r.get('base_sha', '?')}`",
                      f"- score: {r['score']}  risk: {r.get('risk', '?')}  decision: {r.get('decision', '?')}",
                      metrics_line,
                      f"- feedback: {r['feedback']}",
                      f"- reflection: {r.get('reflection', '')}", ""]
        out = self.run_dir / "final_report.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        return out


def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        if "candidates" in r:
            for c in r.get("candidates") or []:
                row = dict(c)
                row["round"] = r.get("round")
                yield row
        else:
            yield r
