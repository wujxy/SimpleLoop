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
    interrupted: bool = False
    interruption: str | None = None


class RoundRunner(Protocol):
    def run(self, request: RoundRequest) -> RoundResult: ...


class RsiRunner(Protocol):
    def due(self, round_id: int) -> bool: ...
    def run(self, round_id: int) -> RsiResult: ...


class RoundHistory(Protocol):
    def append_round(self, result: RoundResult) -> None: ...


class CheckpointStore(Protocol):
    def clear(self) -> None: ...


class LoopObserver(Protocol):
    def round_committed(self, result: RoundResult) -> None: ...


def run_loop(
    request: LoopRequest,
    *,
    rounds: RoundRunner,
    rsi: RsiRunner,
    history: RoundHistory,
    checkpoint: CheckpointStore,
    observer: LoopObserver,
) -> LoopResult:
    state = request.state
    task_rounds = 0
    rsi_rounds = 0
    rsi_tally: dict[str, int] = {}
    while state.next_round < request.stop_round:
        round_id = state.next_round
        try:
            if rsi.due(round_id):
                rsi_result = rsi.run(round_id)
                rsi_rounds += 1
                rsi_tally[rsi_result.decision] = (
                    rsi_tally.get(rsi_result.decision, 0) + 1
                )
                state = LoopState(
                    round_id + 1,
                    state.incumbent_sha,
                    state.incumbent_metrics,
                )
                continue
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
                interrupted=True, interruption=str(exc),
            )
        history.append_round(result)
        checkpoint.clear()
        observer.round_committed(result)
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
    return LoopResult(state, task_rounds, rsi_rounds, rsi_tally)
