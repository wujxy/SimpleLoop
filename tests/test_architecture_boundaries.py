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
        "simpleloop/execution/backend.py",
        "simpleloop/scheduling/worker.py",
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
        "simpleloop/scheduling/handlers/candidate.py",
        "simpleloop/execution/backend.py",
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


def test_legacy_scheduling_owners_are_removed():
    for relative in (
        "simpleloop/candidate_worker.py",
        "simpleloop/proposer_lane_worker.py",
        "simpleloop/execution/base.py",
        "simpleloop/execution/local.py",
        "simpleloop/execution/hepjob.py",
        "simpleloop/execution/proposer_lanes.py",
    ):
        assert not (ROOT / relative).exists(), relative


def test_phase4_has_one_result_protocol_and_one_inflight_journal():
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "simpleloop").rglob("*.py")
    )
    for legacy in (
        "_FINISHED", "usage.json", "inflight_round.json",
        "inflight_proposer.json",
    ):
        assert legacy not in production
    assert production.count('PROTOCOL = "simpleloop.worker.v1"') == 1
    assert production.count('SCHEMA = "simpleloop.inflight.v1"') == 1


def test_condor_commands_are_owned_only_by_config_and_scheduler():
    owners = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "simpleloop").rglob("*.py")
        if any(command in path.read_text(encoding="utf-8") for command in (
            "condor_submit", "condor_q", "condor_rm",
        ))
    }
    assert owners <= {
        "simpleloop/config.py", "simpleloop/scheduling/hepjob.py",
    }


def test_standalone_proposer_does_not_import_simpleloop():
    for path in (ROOT / "proposer").rglob("*.py"):
        imports = imported_modules(path)
        assert not any(
            name == "simpleloop" or name.startswith("simpleloop.")
            for name in imports
        ), path


def test_business_modules_depend_only_on_world_contracts():
    for relative in (
        "simpleloop/candidate.py",
        "simpleloop/roles/agent.py",
        "simpleloop/stages/artifacts.py",
        "simpleloop/stages/executor.py",
        "simpleloop/stages/evaluator.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "container.runtime" not in source, relative
        assert "world.git" not in source, relative
        assert "Apptainer" not in source, relative


def test_old_world_owners_are_removed():
    assert not (ROOT / "simpleloop/container/runtime.py").exists()
    assert not (ROOT / "simpleloop/harness/workspace.py").exists()


def test_proposer_package_remains_independent_of_simpleloop_world():
    for path in (ROOT / "proposer").rglob("*.py"):
        assert "simpleloop.world" not in path.read_text(encoding="utf-8"), path
