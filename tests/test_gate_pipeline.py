import pytest

from simpleloop.harness import gate


SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "FCN"}, {"key": "CONSISTENCY"}],
}


def test_build_results_records_harness_and_configured_gates():
    results = gate.build_results(
        SCHEMA,
        eval_commands=False,
        eval_detail="exit codes: [7]",
        metrics={"FCN": True, "CONSISTENCY": False},
    )

    assert results == {
        "EVAL_COMMANDS": {"passed": False, "detail": "exit codes: [7]"},
        "FCN": {"passed": True, "detail": ""},
        "CONSISTENCY": {
            "passed": False,
            "detail": "evaluator reported FAIL",
        },
    }
    assert gate.all_passed(results) is False


def test_build_results_treats_missing_metric_as_unknown():
    results = gate.build_results(
        SCHEMA,
        eval_commands=True,
        metrics={"FCN": True},
    )

    assert results["CONSISTENCY"] == {
        "passed": None,
        "detail": "metric missing or unknown",
    }
    assert gate.all_passed(results) is False


@pytest.mark.parametrize("key", ["EVAL_COMMANDS", "FCN"])
def test_build_results_rejects_reserved_or_duplicate_gate_names(key: str):
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "FCN"}, {"key": key}],
    }

    with pytest.raises(ValueError, match="gate key"):
        gate.build_results(schema, eval_commands=False)
