"""Executable guards for the Phase 1 Host/Kernel boundaries."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_host_pipeline_does_not_import_proposer_package():
    for relative in (
        "simpleloop/loop.py",
        "simpleloop/execution/proposer_lanes.py",
        "simpleloop/stages/proposer.py",
    ):
        imports = imported_modules(ROOT / relative)
        assert not any(
            name == "proposer" or name.startswith("proposer.")
            for name in imports
        ), relative


PIPELINE_FUNCTIONS = {
    "_run_locked",
    "_finalize_candidates",
    "_run_candidates",
    "_run_candidate_guarded",
    "_run_one_candidate",
    "_candidate_failure",
    "_print_round_performance",
}
CANDIDATE_NAMES = {"candidate", "winner", "first", "best"}


def test_current_round_pipeline_does_not_index_candidate_dicts():
    tree = ast.parse(
        (ROOT / "simpleloop/loop.py").read_text(encoding="utf-8")
    )
    violations = []
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in PIPELINE_FUNCTIONS
    ):
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in CANDIDATE_NAMES
            ):
                violations.append((function.name, node.lineno, node.value.id))
    assert violations == []


def test_candidate_execution_modules_do_not_import_loop():
    for relative in (
        "simpleloop/candidate.py",
        "simpleloop/candidate_worker.py",
        "simpleloop/execution/local.py",
        "simpleloop/execution/hepjob.py",
        "simpleloop/stages/executor.py",
        "simpleloop/stages/evaluator.py",
        "simpleloop/stages/gate.py",
    ):
        imports = imported_modules(ROOT / relative)
        assert "loop" not in imports
        assert "simpleloop.loop" not in imports


def test_loop_does_not_define_candidate_execution_helpers():
    tree = ast.parse(
        (ROOT / "simpleloop/loop.py").read_text(encoding="utf-8")
    )
    names = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not names.intersection({
        "_run_candidates",
        "_run_candidate_guarded",
        "_deps_from_ctx",
        "_run_one_candidate",
        "_candidate_failure",
    })


def test_candidate_pipeline_has_no_context_or_config_bundle():
    source = (ROOT / "simpleloop/candidate.py").read_text(encoding="utf-8")
    assert "RunContext" not in source
    assert "CandidateDeps" not in source
    assert ".cfg" not in source


def test_migrated_candidate_owners_are_removed():
    assert not (ROOT / "simpleloop/roles/executor.py").exists()
    assert not (ROOT / "simpleloop/harness/gate.py").exists()
