"""History store: append-only JSONL of each round + best-score tracking.

Each round records {round, proposal, sha, score, feedback, eval_block}. The best
commit is the round with the highest score seen so far (judger's subjective 0-1
score). This is the loop's output AND the next proposer's input.

eval_block is the raw harness-run eval output for the round (capped), stored so
the loop can feed the prior round's eval + the baseline eval to the next round's
judger as explicit comparison axes — otherwise the judger has only the current
round's single absolute number and can't tell "vs prior round" from "vs baseline"
(see memory simpleloop-judger-prior-round-compare). Storing the raw text (not a
parsed metric) keeps this project-agnostic: the judger reads the number out of
the text itself, no SPEED_MS=/CORRECTNESS= hardcoding in the loop.

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
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "history.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.best_score: float = -1.0
        self.best_sha: str | None = None
        self.best_round: int | None = None

    def append(self, round_id: int, proposal: str, sha: str | None,
               score: float | None, feedback: str,
               eval_block: str = "") -> None:
        """Record one round. Updates best if this round's score beats it."""
        record = {
            "round": round_id,
            "proposal": proposal,
            "sha": sha,
            "score": score,
            "feedback": feedback,
            "eval_block": eval_block[:_HIST_EVAL_CAP],
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if sha and score is not None and score > self.best_score:
            self.best_score = score
            self.best_sha = sha
            self.best_round = round_id

    def history(self) -> list[dict]:
        """Read all rounds back (for the proposer's prompt)."""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def last_eval_block(self) -> str | None:
        """The most recent round's eval_block, or None if no rounds yet.

        The loop uses this as the 'prior round' comparison axis for the next
        round's judger (round 0's prior falls back to the baseline eval).
        """
        rounds = self.history()
        if not rounds:
            return None
        return rounds[-1].get("eval_block") or None

    def write_final_report(self, goal: str) -> Path:
        """Write a human-readable markdown summary. Returns its path."""
        rounds = self.history()
        lines = [f"# SimpleLoop Run Report", "", f"**Goal:** {goal}", ""]
        if self.best_sha:
            lines += [f"- Best commit: `{self.best_sha}` (round {self.best_round}, score {self.best_score:.2f})", ""]
        else:
            lines += ["- No accepted commit produced.", ""]
        lines += ["## Round History", ""]
        for r in rounds:
            lines += [f"### Round {r['round']}", f"- proposal: {r['proposal'][:200]}",
                      f"- sha: `{r['sha']}`", f"- score: {r['score']}",
                      f"- feedback: {r['feedback'][:300]}", ""]
        out = self.run_dir / "final_report.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        return out
