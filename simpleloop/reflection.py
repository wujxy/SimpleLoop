"""Host-owned Reflection log and loop-facing pipeline.

Reflection is the Scientist's periodic anti-inertia checkpoint (continuity
design §16): every ``interval_rounds`` rounds the loop spends one round id on
a session that audits the recent research trajectory instead of advancing
it. Distinct from RSI — reflection changes the Scientist's cognitive state,
not the Scientist's body; nothing here touches the self event stream.

This module is host-only: it must not import the ``proposer`` package
(pinned by tests/test_architecture_boundaries.py). The proposer reads
``run_dir/reflection/history.jsonl`` as data, exactly as it reads
``run_dir/history.jsonl``.

Scheduling state is DERIVED, not stored: the log of completed reflections is
the only durable state, and the next reflection round is
``last_reflection_round + interval`` (or ``first_reflection_round`` when the
log is empty). A ``--continue`` after a crash needs no reconciliation beyond
``prepare`` clearing a stale inflight record whose round is already logged.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReflectionRecord:
    round_id: int
    handoff: str
    self_limitation_suspected: bool = False
    abstained: bool = False
    note: str = ""


class JsonlReflectionLog:
    """Append-only log of completed reflections under ``run_dir/reflection``.

    Mirrors the self history store's durability discipline (fsync per append);
    tolerant of torn/blank lines on read so a crash mid-append never breaks
    the next round's scheduling or context assembly.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "history.jsonl"

    def records(self) -> tuple[ReflectionRecord, ...]:
        if not self.path.exists():
            return ()
        out: list[ReflectionRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            round_id = obj.get("round_id")
            if not isinstance(round_id, int) or isinstance(round_id, bool):
                continue
            out.append(ReflectionRecord(
                round_id,
                str(obj.get("handoff") or ""),
                bool(obj.get("self_limitation_suspected")),
                bool(obj.get("abstained")),
                str(obj.get("note") or ""),
            ))
        return tuple(out)

    def last_round(self) -> int | None:
        records = self.records()
        return records[-1].round_id if records else None

    def has_round(self, round_id: int) -> bool:
        return any(record.round_id == round_id for record in self.records())

    def append(self, record: ReflectionRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "round_id": record.round_id,
                "handoff": record.handoff,
                "self_limitation_suspected": record.self_limitation_suspected,
                "abstained": record.abstained,
                "note": record.note,
            }, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def next_reflection_round(
    *, log: JsonlReflectionLog,
    interval_rounds: int, first_reflection_round: int,
) -> int:
    """The next round id reflection is due at. Derived from the log — no
    state file (see module docstring)."""
    last = log.last_round()
    if last is None:
        return first_reflection_round
    return last + interval_rounds


class ReflectionPipeline:
    """The loop-facing port, mirroring RsiPipeline's due/run contract."""

    def __init__(
        self, *, run_dir: Path, workspace, reflector, log, checkpoint,
        interval_rounds: int, first_reflection_round: int,
    ):
        self.run_dir = Path(run_dir)
        self.workspace = workspace
        self.reflector = reflector
        self.log = log
        self.checkpoint = checkpoint
        self.interval_rounds = interval_rounds
        self.first_reflection_round = first_reflection_round

    def prepare(self) -> None:
        """Clear a stale inflight reflection record whose round is already in
        the log (the reflection completed before the crash). An inflight
        record for a round NOT in the log is left for the reflector to reuse
        — same recovery shape as the RSI pipeline."""
        record = self.checkpoint.inflight()
        if (
            record is not None
            and str(record.stage) == "reflection"
            and self.log.has_round(int(record.round_id))
        ):
            self.checkpoint.clear()

    def due(self, round_id: int) -> bool:
        # Fixed-N anti-inertia backstop only. (An earlier trigger is
        # derivable from a history fold — consecutive rounds without a new
        # incumbent, expectation-miss streaks — if a future version wants
        # it; deliberately not built now.)
        return round_id >= next_reflection_round(
            log=self.log,
            interval_rounds=self.interval_rounds,
            first_reflection_round=self.first_reflection_round,
        )

    def incumbent_sha(self) -> str:
        """The accepted revision the reflection should study: the last
        selected_sha in the task history, else the workspace baseline."""
        history_path = self.run_dir / "history.jsonl"
        if history_path.exists():
            last_selected = None
            for line in history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("selected_sha"):
                    last_selected = row["selected_sha"]
            if last_selected:
                return str(last_selected)
        return self.workspace.baseline_sha()

    def run(self, round_id: int) -> ReflectionRecord:
        # Idempotent: a retried round whose record is already logged returns
        # it instead of reflecting twice.
        for record in self.log.records():
            if record.round_id == round_id:
                return record
        record = self.reflector.reflect(round_id, self.incumbent_sha())
        self.log.append(record)
        self.checkpoint.clear()
        return record
