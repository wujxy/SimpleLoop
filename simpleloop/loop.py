"""Typed state progression for task and RSI rounds."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from .round import RoundRequest, RoundResult, SelectionPolicy
from .scheduling.contracts import InfrastructureError


@dataclass(frozen=True)
class LoopState:
    next_round: int
    incumbent_sha: str
    incumbent_metrics: Mapping[str, object]


@dataclass(frozen=True)
class LoopRequest:
    goal: str
    stop_round: int
    state: LoopState
    selection: SelectionPolicy
    # A task round that commits no candidates (proposer abstain OR protocol
    # failure) is a wasted round id. A few in a row is a systemic proposer/
    # provider problem, not research noise — stop the run with a diagnosis
    # instead of burning the remaining budget on empty rounds.
    max_empty_task_rounds: int = 3


@dataclass(frozen=True)
class RsiResult:
    round_id: int
    decision: str


@dataclass(frozen=True)
class LoopResult:
    state: LoopState
    task_rounds: int
    rsi_rounds: int
    rsi_tally: Mapping[str, int]
    reflection_rounds: int = 0
    interrupted: bool = False
    interruption: str | None = None


class RoundRunner(Protocol):
    def run(self, request: RoundRequest) -> RoundResult: ...


class RsiRunner(Protocol):
    def due(self, round_id: int) -> bool: ...
    def run(self, round_id: int) -> RsiResult: ...


class ReflectionRunner(Protocol):
    """A periodic reflection checkpoint (continuity design §16): consumes a
    round id, leaves the incumbent unchanged, and returns the number of the
    round it occupied."""

    def due(self, round_id: int) -> bool: ...
    def run(self, round_id: int) -> int: ...
    # ``defer`` is an OPTIONAL hook (called via getattr): a runner that knows
    # its interval can push a FAILED reflection to the next interval so the
    # loop does not retry it every round.


class RoundHistory(Protocol):
    def append_round(self, result: RoundResult) -> None: ...


class CheckpointStore(Protocol):
    def clear(self) -> None: ...


class LoopObserver(Protocol):
    """Frontend progress sink.

    ``round_started`` and ``rsi_finished`` are optional hooks: ``run_loop``
    calls them only when the observer provides them, so a minimal observer
    (and test fakes) may implement ``round_committed`` alone.
    ``reflection_started`` / ``reflection_finished`` /
    ``reflection_failed`` follow the same rule.
    """

    def round_started(self, round_id: int, *, rsi: bool) -> None: ...
    def round_committed(self, result: RoundResult) -> None: ...
    def rsi_finished(self, result: RsiResult) -> None: ...
    def reflection_started(self, round_id: int) -> None: ...
    def reflection_finished(self, round_id: int) -> None: ...
    def reflection_failed(self, round_id: int, detail: str) -> None: ...


def _notify(observer: LoopObserver, hook: str, *args, **kwargs) -> None:
    method = getattr(observer, hook, None)
    if callable(method):
        method(*args, **kwargs)


def run_loop(
    request: LoopRequest,
    *,
    rounds: RoundRunner,
    rsi: RsiRunner,
    history: RoundHistory,
    checkpoint: CheckpointStore,
    observer: LoopObserver,
    reflection: ReflectionRunner | None = None,
) -> LoopResult:
    state = request.state
    task_rounds = 0
    rsi_rounds = 0
    reflection_rounds = 0
    rsi_tally: dict[str, int] = {}
    empty_task_streak = 0
    while state.next_round < request.stop_round:
        round_id = state.next_round
        try:
            if rsi.due(round_id):
                _notify(observer, "round_started", round_id, rsi=True)
                rsi_result = rsi.run(round_id)
                rsi_rounds += 1
                rsi_tally[rsi_result.decision] = (
                    rsi_tally.get(rsi_result.decision, 0) + 1
                )
                _notify(observer, "rsi_finished", rsi_result)
                state = LoopState(
                    round_id + 1,
                    state.incumbent_sha,
                    state.incumbent_metrics,
                )
                continue
            if reflection is not None and reflection.due(round_id):
                _notify(observer, "reflection_started", round_id)
                try:
                    reflection.run(round_id)
                    reflection_rounds += 1
                    _notify(observer, "reflection_finished", round_id)
                except InfrastructureError as exc:
                    # A failed reflection (worker/protocol/provider error)
                    # must not kill the run: it leaves no record, so the
                    # derived schedule would retry it every round — hand the
                    # runner a chance to defer to its next interval instead.
                    defer = getattr(reflection, "defer", None)
                    if callable(defer):
                        defer(round_id)
                    _notify(
                        observer, "reflection_failed", round_id, str(exc),
                    )
                state = LoopState(
                    round_id + 1,
                    state.incumbent_sha,
                    state.incumbent_metrics,
                )
                continue
            _notify(observer, "round_started", round_id, rsi=False)
            result = rounds.run(RoundRequest(
                round_id,
                request.goal,
                state.incumbent_sha,
                state.incumbent_metrics,
                request.selection,
            ))
        except InfrastructureError as exc:
            return LoopResult(
                state, task_rounds, rsi_rounds, rsi_tally,
                reflection_rounds=reflection_rounds,
                interrupted=True, interruption=str(exc),
            )
        history.append_round(result)
        checkpoint.clear()
        observer.round_committed(result)
        if result.candidates:
            empty_task_streak = 0
        else:
            empty_task_streak += 1
            if empty_task_streak >= request.max_empty_task_rounds:
                reason = getattr(
                    getattr(result.proposals, "abstention", None),
                    "reason", None,
                )
                # The round DID commit to history — a --continue must resume
                # after it, and the count must include it.
                return LoopResult(
                    LoopState(
                        round_id + 1,
                        state.incumbent_sha,
                        state.incumbent_metrics,
                    ),
                    task_rounds + 1, rsi_rounds, rsi_tally,
                    reflection_rounds=reflection_rounds,
                    interrupted=True,
                    interruption=(
                        f"{empty_task_streak} consecutive task rounds "
                        "produced no candidates"
                        + (f" (last abstain: {reason})" if reason else "")
                        + " — proposer/provider failure suspected; stopping "
                        "instead of burning rounds"
                    ),
                )
        winner = next((
            candidate for candidate in result.candidates
            if candidate.candidate_id == result.selection.candidate_id
        ), None)
        state = LoopState(
            round_id + 1,
            result.next_sha,
            dict(winner.metrics) if winner is not None else state.incumbent_metrics,
        )
        task_rounds += 1
    return LoopResult(
        state, task_rounds, rsi_rounds, rsi_tally, reflection_rounds,
    )
