import pytest

from simpleloop.candidate import EvaluationResult
from simpleloop.stages.gate import GateSpec, apply_gates


SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "FCN"}, {"key": "CONSISTENCY"}],
}


def spec(schema=SCHEMA):
    return GateSpec(
        schema["objective"]["key"],
        tuple(item["key"] for item in schema["gates"]),
    )


def test_apply_gates_records_harness_and_configured_gates():
    decision = apply_gates(
        EvaluationResult(
            "",
            {"FCN": True, "CONSISTENCY": False},
            (7,),
        ),
        spec(),
    )

    assert decision.results["PATHS"].passed is True
    assert decision.results["EVAL_COMMANDS"].passed is False
    assert decision.results["EVAL_COMMANDS"].detail == "exit codes: [7]"
    assert decision.results["FCN"].passed is True
    assert decision.results["CONSISTENCY"].passed is False
    assert decision.results["CONSISTENCY"].detail == "evaluator reported FAIL"
    assert decision.passed is False


def test_apply_gates_marks_skipped_gates_unknown():
    decision = apply_gates(
        None,
        spec(),
        skip_reason="not run because execution failed",
    )

    assert decision.results["PATHS"].passed is True
    assert decision.results["EVAL_COMMANDS"].passed is None
    assert decision.results["EVAL_COMMANDS"].detail == (
        "not run because execution failed"
    )
    assert decision.results["FCN"].passed is None


def test_apply_gates_treats_missing_metric_as_unknown():
    decision = apply_gates(
        EvaluationResult("", {"SPEED_MS": 1.0, "FCN": True}, (0,)),
        spec(),
    )

    assert decision.results["CONSISTENCY"].passed is None
    assert decision.results["CONSISTENCY"].detail == "metric missing or unknown"
    assert decision.passed is False


@pytest.mark.parametrize("key", ["PATHS", "EVAL_COMMANDS", "FCN"])
def test_apply_gates_rejects_reserved_or_duplicate_gate_names(key: str):
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "FCN"}, {"key": key}],
    }

    with pytest.raises(ValueError, match="gate key"):
        apply_gates(None, spec(schema))
