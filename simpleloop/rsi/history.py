"""Append-only authority and projections for one run-local self history."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .models import (
    SelfChange,
    SelfDecision,
    SelfDecisionKind,
    SelfEvent,
    SelfEventKind,
    SelfState,
)


_SCHEMA_VERSION = 1


class SelfHistoryConflictError(RuntimeError):
    pass


class JsonlSelfHistoryStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "history.jsonl"
        self.review_view_path = self.root / "reviews.jsonl"

    def initialize(
        self, active_sha: str, first_review_round: int | None,
    ) -> SelfState:
        events = self.events()
        if not events:
            self.append(initialized_event(active_sha, first_review_round))
        elif events[0].incumbent_sha != active_sha:
            raise SelfHistoryConflictError(
                "self body seed does not match INITIALIZED event"
            )
        self.ensure_review_view()
        return self.state()

    def events(self) -> tuple[SelfEvent, ...]:
        if not self.path.exists():
            return ()
        rows = []
        for line_no, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                rows.append(_decode_event(raw))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid self history event at line {line_no}: {exc}"
                ) from exc
        return tuple(rows)

    def state(self) -> SelfState:
        state, _ = _project(self.events())
        return state

    def append(self, event: SelfEvent) -> None:
        current = self.events()
        for existing in current:
            if existing.event_id != event.event_id:
                continue
            if existing == event:
                self.ensure_review_view()
                return
            raise SelfHistoryConflictError(
                f"conflicting self event id {event.event_id}"
            )
        combined = (*current, event)
        _, reviews = _project(combined)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                _encode_event(event), ensure_ascii=False, sort_keys=True,
            ) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._write_reviews(reviews)

    def ensure_review_view(self) -> Path:
        _, reviews = _project(self.events())
        expected = _review_text(reviews)
        if (
            not self.review_view_path.exists()
            or self.review_view_path.read_text(encoding="utf-8") != expected
        ):
            self._write_reviews(reviews)
        return self.review_view_path

    def _write_reviews(self, reviews: list[dict[str, object]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.review_view_path.with_suffix(".jsonl.tmp")
        temporary.write_text(_review_text(reviews), encoding="utf-8")
        temporary.replace(self.review_view_path)


def initialized_event(
    active_sha: str, first_review_round: int | None,
) -> SelfEvent:
    return SelfEvent(
        "self:INITIALIZED", SelfEventKind.INITIALIZED, -1, active_sha,
        next_review_round=first_review_round,
    )


def reviewed_keep_event(
    round_id: int,
    incumbent_sha: str,
    decision: SelfDecision,
    next_review_round: int,
) -> SelfEvent:
    return SelfEvent(
        _event_id(round_id, SelfEventKind.REVIEWED_KEEP),
        SelfEventKind.REVIEWED_KEEP,
        round_id,
        incumbent_sha,
        decision=decision,
        next_review_round=next_review_round,
    )


def reviewed_change_event(
    round_id: int, incumbent_sha: str, decision: SelfDecision,
) -> SelfEvent:
    return SelfEvent(
        _event_id(round_id, SelfEventKind.REVIEWED_CHANGE),
        SelfEventKind.REVIEWED_CHANGE,
        round_id,
        incumbent_sha,
        decision=decision,
    )


def candidate_created_event(
    round_id: int,
    incumbent_sha: str,
    candidate_sha: str,
    detail: str = "",
) -> SelfEvent:
    return SelfEvent(
        _event_id(round_id, SelfEventKind.CANDIDATE_CREATED),
        SelfEventKind.CANDIDATE_CREATED,
        round_id,
        incumbent_sha,
        candidate_sha=candidate_sha,
        detail=detail,
    )


def candidate_rejected_event(
    round_id: int,
    incumbent_sha: str,
    next_review_round: int,
    detail: str,
    candidate_sha: str | None = None,
) -> SelfEvent:
    return SelfEvent(
        _event_id(round_id, SelfEventKind.CANDIDATE_REJECTED),
        SelfEventKind.CANDIDATE_REJECTED,
        round_id,
        incumbent_sha,
        candidate_sha=candidate_sha,
        next_review_round=next_review_round,
        viable=False,
        detail=detail,
    )


def candidate_adopted_event(
    round_id: int,
    incumbent_sha: str,
    candidate_sha: str,
    next_review_round: int,
    detail: str,
) -> SelfEvent:
    return SelfEvent(
        _event_id(round_id, SelfEventKind.CANDIDATE_ADOPTED),
        SelfEventKind.CANDIDATE_ADOPTED,
        round_id,
        incumbent_sha,
        candidate_sha=candidate_sha,
        next_review_round=next_review_round,
        viable=True,
        detail=detail,
    )


def _event_id(round_id: int, kind: SelfEventKind) -> str:
    return f"r{round_id}:{kind.value}"


def _project(
    events: tuple[SelfEvent, ...],
) -> tuple[SelfState, list[dict[str, object]]]:
    if not events:
        raise ValueError("self history is not initialized")
    first = events[0]
    if first.kind is not SelfEventKind.INITIALIZED:
        raise ValueError("self history must start with INITIALIZED")
    active = first.incumbent_sha
    next_review = first.next_review_round
    last_review = None
    pending: dict[int, SelfDecision] = {}
    candidates: dict[int, str] = {}
    reviews: list[dict[str, object]] = []
    for event in events[1:]:
        if event.kind is SelfEventKind.INITIALIZED:
            raise ValueError("self history contains multiple INITIALIZED events")
        if event.incumbent_sha != active:
            raise ValueError(
                f"self event {event.event_id} incumbent does not match active sha"
            )
        if event.kind is SelfEventKind.REVIEWED_KEEP:
            decision = _expect_decision(event, SelfDecisionKind.KEEP)
            _expect_new_round(event.round_id, pending, last_review)
            next_review = _required_next(event)
            last_review = event.round_id
            reviews.append(_review_row(event, decision, adopted=None))
        elif event.kind is SelfEventKind.REVIEWED_CHANGE:
            decision = _expect_decision(event, SelfDecisionKind.CHANGE)
            _expect_new_round(event.round_id, pending, last_review)
            pending[event.round_id] = decision
        elif event.kind is SelfEventKind.CANDIDATE_CREATED:
            _pending_change(event.round_id, pending)
            if not event.candidate_sha:
                raise ValueError("CANDIDATE_CREATED requires candidate_sha")
            candidates[event.round_id] = event.candidate_sha
        elif event.kind is SelfEventKind.CANDIDATE_REJECTED:
            decision = _pending_change(event.round_id, pending)
            candidate = candidates.get(event.round_id)
            if candidate and event.candidate_sha not in {None, candidate}:
                raise ValueError("rejected candidate does not match created candidate")
            next_review = _required_next(event)
            last_review = event.round_id
            reviews.append(_review_row(event, decision, adopted=False))
            pending.pop(event.round_id)
            candidates.pop(event.round_id, None)
        elif event.kind is SelfEventKind.CANDIDATE_ADOPTED:
            decision = _pending_change(event.round_id, pending)
            candidate = candidates.get(event.round_id)
            if candidate is None:
                raise ValueError("adoption requires a CANDIDATE_CREATED event")
            if event.candidate_sha != candidate:
                raise ValueError("adopted candidate does not match created candidate")
            next_review = _required_next(event)
            active = candidate
            last_review = event.round_id
            reviews.append(_review_row(event, decision, adopted=True))
            pending.pop(event.round_id)
            candidates.pop(event.round_id, None)
    return SelfState(active, next_review, last_review), reviews


def _expect_new_round(
    round_id: int,
    pending: Mapping[int, SelfDecision],
    last_review: int | None,
) -> None:
    if round_id in pending or (last_review is not None and round_id <= last_review):
        raise ValueError(f"self round {round_id} is already recorded")


def _pending_change(
    round_id: int, pending: Mapping[int, SelfDecision],
) -> SelfDecision:
    decision = pending.get(round_id)
    if decision is None:
        raise ValueError(f"round {round_id} has no reviewed CHANGE")
    return decision


def _expect_decision(
    event: SelfEvent, expected: SelfDecisionKind,
) -> SelfDecision:
    decision = event.decision
    if decision is None or decision.kind is not expected:
        raise ValueError(f"{event.kind.value} requires a {expected.value} decision")
    return decision


def _required_next(event: SelfEvent) -> int:
    value = event.next_review_round
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{event.kind.value} requires next_review_round")
    return value


def _review_row(
    event: SelfEvent,
    decision: SelfDecision,
    *,
    adopted: bool | None,
) -> dict[str, object]:
    return {
        "round": event.round_id,
        "incumbent_self_sha": event.incumbent_sha,
        "decision": decision.kind.value,
        "diagnosis": decision.diagnosis,
        "keep_reason": decision.keep_reason,
        "change": _encode_change(decision.change),
        "candidate_self_sha": event.candidate_sha,
        "viable": event.viable,
        "adopted": adopted,
        "next_review_round": event.next_review_round,
        "detail": event.detail,
    }


def _review_text(rows: list[dict[str, object]]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    )


def _encode_event(event: SelfEvent) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "event_id": event.event_id,
        "kind": event.kind.value,
        "round": event.round_id,
        "incumbent_sha": event.incumbent_sha,
        "decision": _encode_decision(event.decision),
        "candidate_sha": event.candidate_sha,
        "next_review_round": event.next_review_round,
        "viable": event.viable,
        "detail": event.detail,
    }


def _decode_event(raw: object) -> SelfEvent:
    if not isinstance(raw, Mapping):
        raise TypeError("event must be an object")
    if raw.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported self history schema_version")
    return SelfEvent(
        str(raw.get("event_id") or ""),
        SelfEventKind(str(raw.get("kind") or "")),
        int(raw.get("round")),
        str(raw.get("incumbent_sha") or ""),
        decision=_decode_decision(raw.get("decision")),
        candidate_sha=(
            str(raw["candidate_sha"]) if raw.get("candidate_sha") else None
        ),
        next_review_round=(
            int(raw["next_review_round"])
            if raw.get("next_review_round") is not None else None
        ),
        viable=raw.get("viable") if isinstance(raw.get("viable"), bool) else None,
        detail=str(raw.get("detail") or ""),
    )


def _encode_decision(decision: SelfDecision | None) -> object:
    if decision is None:
        return None
    return {
        "kind": decision.kind.value,
        "diagnosis": decision.diagnosis,
        "keep_reason": decision.keep_reason,
        "change": _encode_change(decision.change),
        "next_review_after_rounds": decision.next_review_after_rounds,
    }


def _decode_decision(raw: object) -> SelfDecision | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError("decision must be an object")
    defer = raw.get("next_review_after_rounds")
    return SelfDecision(
        SelfDecisionKind(str(raw.get("kind") or "")),
        str(raw.get("diagnosis") or ""),
        keep_reason=(
            str(raw["keep_reason"]) if raw.get("keep_reason") is not None else None
        ),
        change=_decode_change(raw.get("change")),
        next_review_after_rounds=int(defer) if defer is not None else None,
    )


def _encode_change(change: SelfChange | None) -> object:
    if change is None:
        return None
    return {
        "target": change.target,
        "intent": change.intent,
        "instruction": change.instruction,
        "evidence_refs": list(change.evidence_refs),
    }


def _decode_change(raw: object) -> SelfChange | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError("change must be an object")
    return SelfChange(
        str(raw.get("target") or ""),
        str(raw.get("intent") or ""),
        str(raw.get("instruction") or ""),
        tuple(str(item) for item in raw.get("evidence_refs") or ()),
    )
