"""Provider-free RSI transaction and its loop-facing adapter."""
from __future__ import annotations

from pathlib import Path

from .history import (
    candidate_adopted_event,
    candidate_created_event,
    candidate_rejected_event,
    reviewed_change_event,
    reviewed_keep_event,
)
from .models import (
    NoSelfChangeError,
    RsiRequest,
    RsiResult,
    SelfCommitRequest,
    SelfDecision,
    SelfDecisionKind,
    SelfEditRequest,
    SelfEvent,
    SelfEventKind,
    SelfReviewRequest,
    ViabilityRequest,
)


_DEFAULT_REVIEW_DEFER = 8
_TERMINAL_KINDS = frozenset({
    SelfEventKind.REVIEWED_KEEP,
    SelfEventKind.CANDIDATE_REJECTED,
    SelfEventKind.CANDIDATE_ADOPTED,
})


def run_rsi(
    request: RsiRequest,
    *,
    reviewer,
    editor,
    bodies,
    viability,
    history,
) -> RsiResult:
    """Advance one self-review round from its durable event boundary."""
    events = _round_events(history.events(), request.round_id)
    terminal = _terminal(events)
    if terminal is not None:
        return _result(events, terminal)

    state = history.state()
    reviewed = _event(events, SelfEventKind.REVIEWED_CHANGE)
    if reviewed is None:
        runtime = bodies.materialize(state.active_sha)
        decision = reviewer.review(SelfReviewRequest(
            request.round_id,
            request.goal,
            state.active_sha,
            runtime,
            history.review_view_path,
        ))
        next_round = request.round_id + _defer(decision)
        if decision.kind is SelfDecisionKind.KEEP:
            event = reviewed_keep_event(
                request.round_id, state.active_sha, decision, next_round,
            )
            history.append(event)
            return _result((event,), event)
        reviewed = reviewed_change_event(
            request.round_id, state.active_sha, decision,
        )
        history.append(reviewed)
        events = (*events, reviewed)
    decision = _change_decision(reviewed)
    next_round = request.round_id + _defer(decision)
    incumbent_sha = reviewed.incumbent_sha

    workspace = bodies.prepare_candidate(request.round_id, incumbent_sha)
    created = _event(events, SelfEventKind.CANDIDATE_CREATED)
    if created is None:
        edit = editor.edit(SelfEditRequest(
            request.round_id, decision.change, workspace,
        ))
        if edit.status != "EDITED":
            detail = edit.reason or f"self editor returned {edit.status}"
            rejected = candidate_rejected_event(
                request.round_id, incumbent_sha, next_round, detail,
            )
            history.append(rejected)
            bodies.discard_candidate(workspace)
            return _result((*events, rejected), rejected)
        try:
            candidate = bodies.commit_candidate(
                workspace,
                SelfCommitRequest(
                    request.round_id,
                    incumbent_sha,
                    decision.change.target,
                ),
            )
        except NoSelfChangeError as exc:
            rejected = candidate_rejected_event(
                request.round_id, incumbent_sha, next_round, str(exc),
            )
            history.append(rejected)
            bodies.discard_candidate(workspace)
            return _result((*events, rejected), rejected)
        created = candidate_created_event(
            request.round_id,
            incumbent_sha,
            candidate.sha,
            ", ".join(path.as_posix() for path in candidate.changed_paths),
        )
        history.append(created)
        events = (*events, created)

    verdict = viability.check(ViabilityRequest(
        request.round_id,
        created.candidate_sha,
        workspace.path,
    ))
    if verdict.viable:
        terminal = candidate_adopted_event(
            request.round_id,
            incumbent_sha,
            created.candidate_sha,
            next_round,
            verdict.detail,
        )
        history.append(terminal)
        bodies.materialize(created.candidate_sha)
    else:
        terminal = candidate_rejected_event(
            request.round_id,
            incumbent_sha,
            next_round,
            verdict.detail,
            created.candidate_sha,
        )
        history.append(terminal)
    bodies.discard_candidate(workspace)
    return _result((*events, terminal), terminal)


class RsiPipeline:
    """Initialize/recover RSI providers and satisfy the loop's tiny port."""

    def __init__(
        self,
        *,
        goal: str,
        seed: Path,
        first_review_round: int | None,
        reviewer,
        editor,
        bodies,
        viability,
        history,
        checkpoint,
    ):
        self.goal = goal
        self.seed = Path(seed)
        self.first_review_round = first_review_round
        self.reviewer = reviewer
        self.editor = editor
        self.bodies = bodies
        self.viability = viability
        self.history = history
        self.checkpoint = checkpoint

    def prepare(self) -> None:
        initial = self.bodies.initialize(self.seed)
        state = self.history.initialize(
            initial.sha, self.first_review_round,
        )
        self.bodies.materialize(state.active_sha)
        record = self.checkpoint.inflight()
        if (
            record is not None
            and str(record.stage) in {"self_review", "self_edit", "viability"}
            and _terminal(_round_events(
                self.history.events(), int(record.round_id),
            )) is not None
        ):
            self.checkpoint.clear()

    def due(self, round_id: int) -> bool:
        next_round = self.history.state().next_review_round
        return next_round is not None and round_id >= next_round

    def run(self, round_id: int):
        from ..loop import RsiResult as LoopRsiResult

        result = run_rsi(
            RsiRequest(round_id, self.goal),
            reviewer=self.reviewer,
            editor=self.editor,
            bodies=self.bodies,
            viability=self.viability,
            history=self.history,
        )
        self.checkpoint.clear()
        return LoopRsiResult(result.round_id, result.decision.value)


def _round_events(events, round_id: int) -> tuple[SelfEvent, ...]:
    return tuple(event for event in events if event.round_id == round_id)


def _event(
    events: tuple[SelfEvent, ...], kind: SelfEventKind,
) -> SelfEvent | None:
    return next((event for event in events if event.kind is kind), None)


def _terminal(events: tuple[SelfEvent, ...]) -> SelfEvent | None:
    return next((event for event in reversed(events) if event.kind in _TERMINAL_KINDS), None)


def _change_decision(event: SelfEvent) -> SelfDecision:
    decision = event.decision
    if decision is None or decision.kind is not SelfDecisionKind.CHANGE:
        raise ValueError("REVIEWED_CHANGE event has no CHANGE decision")
    return decision


def _defer(decision: SelfDecision) -> int:
    return decision.next_review_after_rounds or _DEFAULT_REVIEW_DEFER


def _result(events: tuple[SelfEvent, ...], terminal: SelfEvent) -> RsiResult:
    if terminal.kind is SelfEventKind.REVIEWED_KEEP:
        decision = terminal.decision
        if decision is None:
            raise ValueError("REVIEWED_KEEP event has no decision")
        return RsiResult(terminal.round_id, decision.kind)
    reviewed = _event(events, SelfEventKind.REVIEWED_CHANGE)
    if reviewed is None:
        raise ValueError("terminal self-change event has no REVIEWED_CHANGE")
    return RsiResult(
        terminal.round_id,
        SelfDecisionKind.CHANGE,
        terminal.candidate_sha,
        terminal.kind is SelfEventKind.CANDIDATE_ADOPTED,
        terminal.detail,
    )
