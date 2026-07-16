"""History store: append-only JSONL of each round + best-score tracking.

Each round records {round, proposal, sha, score, feedback}. The best commit
is the round with the highest score seen so far (judger's subjective 0-1 score).
This is the loop's output AND the next proposer's input.

No separate event/artifact/execution stores — one JSONL covers everything.
"""
from __future__ import annotations

import json
from pathlib import Path


class Store:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "history.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.best_score: float = -1.0
        self.best_sha: str | None = None
        self.best_round: int | None = None

    def append(self, round_id: int, proposal: str, sha: str | None,
               score: float | None, feedback: str) -> None:
        """Record one round. Updates best if this round's score beats it."""
        record = {
            "round": round_id,
            "proposal": proposal,
            "sha": sha,
            "score": score,
            "feedback": feedback,
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
