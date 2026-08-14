"""Loop-resume behavior shared by task and typed RSI rounds."""
from __future__ import annotations

from types import SimpleNamespace

from simpleloop.app import _starting_state


def _task_history(n: int) -> list[dict]:
    return [
        {
            "round": index,
            "selected_sha": f"sha{index}",
            "candidates": [{
                "candidate": index,
                "selected": True,
                "metrics": {"OBJ": index},
            }],
        }
        for index in range(n)
    ]


def _resume_start(history: list[dict], last_self):
    return _starting_state(
        continue_run=True,
        stop_round=10,
        history=history,
        baseline_sha="abcdef1234",
        last_self_review=last_self,
        baseline=SimpleNamespace(evaluate=lambda request: SimpleNamespace(
            metrics={"OBJ": 1.0},
        )),
        telemetry=SimpleNamespace(),
    )


def test_starting_state_resume_after_self_review():
    state, _baseline = _resume_start(_task_history(5), last_self=5)

    assert state.next_round == 6
    assert state.incumbent_sha == "sha4"


def test_starting_state_resume_without_self_review_is_unchanged():
    state, _baseline = _resume_start(_task_history(5), last_self=None)

    assert state.next_round == 5
