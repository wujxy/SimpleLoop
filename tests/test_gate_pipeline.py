from simpleloop.harness import gate


SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "FCN"}, {"key": "CONSISTENCY"}],
}


def test_build_results_records_harness_and_configured_gates():
    results = gate.build_results(
        SCHEMA,
        paths=True,
        eval_commands=False,
        eval_detail="exit codes: [7]",
        metrics={"FCN": True, "CONSISTENCY": False},
    )

    assert results == {
        "PATHS": {"passed": True, "detail": ""},
        "EVAL_COMMANDS": {"passed": False, "detail": "exit codes: [7]"},
        "FCN": {"passed": True, "detail": ""},
        "CONSISTENCY": {
            "passed": False,
            "detail": "evaluator reported FAIL",
        },
    }
    assert gate.all_passed(results) is False


def test_build_results_marks_short_circuited_gates_unknown():
    results = gate.build_results(
        SCHEMA,
        paths=False,
        path_detail="tests/x.py: touches a frozen path",
    )

    assert results["PATHS"]["passed"] is False
    assert results["EVAL_COMMANDS"] == {
        "passed": None,
        "detail": "not run because PATHS failed",
    }
    assert results["FCN"] == {
        "passed": None,
        "detail": "not run because PATHS failed",
    }


def test_build_results_treats_missing_metric_as_unknown():
    results = gate.build_results(
        SCHEMA,
        paths=True,
        eval_commands=True,
        metrics={"FCN": True},
    )

    assert results["CONSISTENCY"] == {
        "passed": None,
        "detail": "metric missing or unknown",
    }
    assert gate.all_passed(results) is False
