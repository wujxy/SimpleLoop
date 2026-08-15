"""Single durable record for the currently active worker stage."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from ..scheduling.envelope import ProtocolError, _atomic_json

import json


def _loads(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"could not parse journal: {exc}") from exc


SCHEMA = "simpleloop.inflight.v1"


@dataclass(frozen=True)
class JournalRecord:
    round_id: int
    stage: str
    context: Mapping[str, object]
    jobs: tuple[Mapping[str, object], ...]


class JobJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def begin(
        self,
        stage: str,
        round_id: int,
        context: Mapping[str, object],
        jobs: Sequence[Mapping[str, object]],
    ) -> JournalRecord:
        current = self.load()
        if current is not None and (
            current.stage != stage or current.round_id != round_id
        ):
            raise ProtocolError(
                f"active stage {current.stage!r} for round {current.round_id} "
                f"cannot be replaced by {stage!r} for round {round_id}"
            )
        record = JournalRecord(round_id, stage, dict(context), tuple(dict(j) for j in jobs))
        self._write(record)
        return record

    def save_jobs(self, jobs: Sequence[Mapping[str, object]]) -> None:
        current = self.load()
        if current is None:
            raise ProtocolError("cannot save jobs without an active stage")
        self._write(JournalRecord(
            current.round_id,
            current.stage,
            current.context,
            tuple(dict(job) for job in jobs),
        ))

    def transition(
        self,
        expected_stage: str,
        stage: str,
        round_id: int,
        context: Mapping[str, object],
        jobs: Sequence[Mapping[str, object]],
    ) -> JournalRecord:
        current = self.load()
        if current is None:
            raise ProtocolError(
                f"expected active stage {expected_stage!r}, found none"
            )
        if current.stage != expected_stage or current.round_id != round_id:
            raise ProtocolError(
                f"expected active stage {expected_stage!r} for round "
                f"{round_id}, found {current.stage!r} for round "
                f"{current.round_id}"
            )
        record = JournalRecord(
            round_id,
            stage,
            dict(context),
            tuple(dict(job) for job in jobs),
        )
        self._write(record)
        return record

    def load(self) -> JournalRecord | None:
        if not self.path.exists():
            return None
        # A blank/zero-byte journal (external truncation — writes are atomic,
        # so a torn write cannot produce this) must not wedge the run at the
        # next round boundary: treat it as no record, loudly. (omilrec-v100
        # -001 died exactly here on a truncated inflight.json.)
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.strip():
            print(
                "[journal] WARNING: inflight journal is blank — "
                "treating it as absent", flush=True,
            )
            return None
        raw = _loads(text)
        if raw.get("schema") != SCHEMA:
            raise ProtocolError(f"unsupported inflight schema: {raw.get('schema')!r}")
        round_id = raw.get("round_id")
        stage = raw.get("stage")
        context = raw.get("context")
        jobs = raw.get("jobs")
        if not isinstance(round_id, int) or isinstance(round_id, bool):
            raise ProtocolError("round_id must be an integer")
        if not isinstance(stage, str) or not stage:
            raise ProtocolError("stage must be a non-empty string")
        if not isinstance(context, dict):
            raise ProtocolError("context must be an object")
        if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
            raise ProtocolError("jobs must be a list of objects")
        return JournalRecord(round_id, stage, context, tuple(jobs))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        if not self.path.parent.is_dir():
            return
        # fsync the directory so a power loss cannot resurrect the record.
        dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _write(self, record: JournalRecord) -> None:
        _atomic_json(self.path, {
            "schema": SCHEMA,
            "round_id": record.round_id,
            "stage": record.stage,
            "context": dict(record.context),
            "jobs": [dict(job) for job in record.jobs],
        })
