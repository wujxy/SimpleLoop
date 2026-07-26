"""History store: append-only JSONL of one generation record per round.

Each generation records {round, parent_sha, selected_candidate, selected_sha,
candidates: [...], reflection, telemetry} plus selected-candidate convenience
fields (proposal, score, risk, feedback, base_sha, ...). The best commit is
selected by the HARNESS, by the real objective metric — NOT by the judger's
subjective 0-1 score. This is the fix for the "best by score" problem surfaced
in the 12-round OMILRECV2 run, where the highest-score round (r5, 0.88, 571ms)
locked out the fastest correct commit (r10, 0.85, 321ms): once the harness owns
the real speed (parsed from eval output), best selection becomes "among
gate-pass + risk-not-high rounds, take the best objective" — score demotes to a
quality signal, not a ranking number.

Best selection rule (metrics_schema is always configured — eval.metrics is a
required config block; there is deliberately no score-based fallback):
  eligible = candidates where every declared gate metric is True (PASS) AND
             judger-risk != "high" AND the objective metric is present+numeric.
  best = the eligible candidate with the best objective (min if lower_is_better,
         else max). First such candidate wins ties.
A failed-gate or high-risk candidate is never best, even if its objective is
best — this is what keeps a latent-risk flag (e.g. r5's MODE-keyed cache
validity) from being the chosen ship commit when a safer later round is nearly
as fast.

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

from . import memory as memory_mod

# Cap the eval_block stored per round. The live eval_block fed to the judger is
# capped separately (run_eval's _OUT_CAP); this cap just keeps history.jsonl from
# ballooning when eval is verbose. 6000 chars is plenty for sl_eval's ~5KB output
# plus headroom, and the judger only needs the result lines (CORRECTNESS=/SPEED_MS=).
_HIST_EVAL_CAP = 6000


class Store:
    def __init__(self, run_dir: Path, metrics_schema: dict):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "history.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # metrics_schema: {"objective": {key, lower_is_better}, "gates": [{key}]}.
        # Always configured (eval.metrics is required). Set once at loop start;
        # drives best selection.
        self.metrics_schema = metrics_schema
        self.best_score: float = -1.0           # judger quality number (report only)
        self.best_sha: str | None = None        # harness-selected best commit
        self.best_round: int | None = None
        self.best_candidate: int | None = None

    def history(self) -> list[dict]:
        """Read all rounds back (for the proposer's prompt)."""
        return memory_mod.read_history(self.path)

    def append_generation(self, round_id: int, *, parent_sha: str,
                          selected_candidate: int | None,
                          selected_sha: str | None,
                          candidates: list[dict],
                          reflection: str = "",
                          telemetry: dict | None = None) -> None:
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
                "feedback_for_proposer": c.get("feedback_for_proposer", ""),
                "eval_block": (c.get("eval_block") or "")[:_HIST_EVAL_CAP],
                "metrics": c.get("metrics") or {},
                "changed_paths": c.get("changed_paths") or [],
                "accepted": bool(c.get("accepted")),
                "selected": c.get("candidate", i) == selected_candidate,
                "telemetry": dict(c.get("telemetry") or {}),
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
            "feedback_for_proposer": (
                selected.get("feedback_for_proposer", "")
                if selected else "[no selected candidate]"
            ),
            "metrics": selected.get("metrics", {}) if selected else {},
            "changed_paths": selected.get("changed_paths", []) if selected else [],
            "accepted": bool(selected_sha),
            "base_sha": selected_sha or parent_sha,
            "reflection": reflection,
            "decision": selected.get("decision", "") if selected else "",
            "candidates": normalized,
            "telemetry": dict(telemetry or {}),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._recompute_best()

    def _recompute_best(self) -> None:
        """Recompute the metric-based best over the full history.

        Done from scratch each append (cheap for tens-of-rounds histories) so
        that a rule change never leaves stale best state.
        """
        rounds = self.history()
        if not rounds:
            return
        candidates = list(_iter_candidates(rounds))
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
            # keep best_score as the judger score exposed by the run summary
            rec = next((r for r in candidates
                        if r["round"] == best[0] and r.get("sha") == best[1]), None)
            if rec and isinstance(rec.get("score"), (int, float)):
                self.best_score = rec["score"]



def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        for c in r.get("candidates") or []:
            row = dict(c)
            row["round"] = r.get("round")
            yield row
