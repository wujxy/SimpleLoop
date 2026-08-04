from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from simpleloop import candidate_worker as worker_mod
from simpleloop import config as config_mod
from simpleloop import loop as loop_mod
from simpleloop.memory import MemoryService
from simpleloop.memory.models import NewFindingTarget, ResearchProposal
from simpleloop.roles.agent import Agent, AgentError, AgentResult
from simpleloop.roles.executor import ExecResult
from simpleloop.harness.evals import EvalResult
from simpleloop.loop import RunContext, _run_candidates, _select_winner
from simpleloop.roles.proposer import ProposerResult
from simpleloop.harness.store import Store, best_candidate


EXAMPLES = Path(__file__).parents[1] / "examples"

_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
           "gates": [{"key": "CORRECTNESS"}]}


def _example_yaml(relative_path: str) -> dict:
    return yaml.safe_load((EXAMPLES / relative_path).read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, loop_block: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"SIF-test-double")
    cfg = {
        "kind": "task",
        "task": {"goal": "go faster"},
        "safety": {"editable_paths": ["src/**"], "frozen_paths": []},
        "loop": {"max_rounds": 3, **(loop_block or {})},
        "runtime": {"image": "runtime.sif"},
        "source": {"path": str(repo), "baseline_ref": "HEAD"},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [{"key": "CORRECTNESS"}],
            },
        },
    }
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_config_parallel_defaults(tmp_path: Path):
    cfg = config_mod.load(_write_config(tmp_path))
    assert cfg["candidates_per_round"] == 1
    assert cfg["max_workers"] == 1


@pytest.mark.parametrize("relative_path", [
    "tiny_algo_opt/task.yaml",
    "omilrec-opt/task.yaml",
    "omilrec-post-v107-opt/task.yaml",
])
def test_fanout_examples_declare_gate_descriptions_and_matched_workers(
    relative_path: str,
):
    raw = _example_yaml(relative_path)

    # Real conformance invariants (not example-specific pins): every gate
    # declares a description the harness renders, and parallel fanout runs as
    # many workers as candidates per round.
    gates = raw["eval"]["metrics"]["gates"]
    assert gates and all("description" in g for g in gates)
    loop = raw["loop"]
    assert loop["candidates_per_round"] == loop["max_workers"] >= 1


@pytest.mark.parametrize("relative_path", [
    "omilrec-opt/task.yaml",
    "omilrec-v100-opt/task.yaml",
    "omilrec-post-v107-opt/task.yaml",
])
def test_default_omilrec_tasks_define_outcomes_not_research_methods(
    relative_path: str,
):
    raw = _example_yaml(relative_path)
    goal = raw["task"]["goal"].lower()

    assert "speed_ms" in goal
    assert "every configured gate" in goal
    for prescribed in (
        "hoisting", "caching", "soa", "safe", "forbidden",
        "second likelihood",
    ):
        assert prescribed not in goal
    assert "OMILRECV2/src/**" in raw["safety"]["editable_paths"]
    assert "OMILRECV2/CMakeLists.txt" in raw["safety"]["editable_paths"]
    assert "OMILRECV2/CMakeLists.txt" not in raw["safety"]["frozen_paths"]


def test_runtime_architecture_has_no_judger_module_or_packaged_prompt():
    root = EXAMPLES.parent

    assert not (root / "simpleloop/roles/judger.py").exists()
    assert not (root / "simpleloop/prompts/judger.md").exists()
    for path in (root / "simpleloop").rglob("*.py"):
        assert "roles.judger" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["candidates_per_round", "max_workers"])
@pytest.mark.parametrize("value", [0, -1, "2"])
def test_config_parallel_rejects_invalid_values(tmp_path: Path, field: str, value):
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(_write_config(tmp_path, {field: value}))


def test_selector_uses_objective_and_filters_ineligible_candidates():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}],
    }
    candidates = [
        {"candidate": 0, "sha": "slow", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 700.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
        {"candidate": 1, "sha": "fast-but-rejected", "gate_passed": False,
         "eligible": False,
         "metrics": {"SPEED_MS": 100.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
        {"candidate": 2, "sha": "fast-fail", "gate_passed": False,
         "eligible": False,
         "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": False, "EVAL_RESULT": True}},
        {"candidate": 3, "sha": "winner", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 650.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
    ]
    assert _select_winner(candidates, schema)["sha"] == "winner"


def test_selector_uses_only_eligibility_objective_and_candidate_order():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 1, "sha": "b", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True}},
        {"candidate": 0, "sha": "a", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True}},
        {"candidate": 2, "sha": "faster-but-rejected",
         "gate_passed": False, "eligible": False,
         "metrics": {"SPEED_MS": 400.0, "CORRECTNESS": False}},
    ]
    assert _select_winner(candidates, schema)["sha"] == "a"


@pytest.mark.parametrize(
    ("lower_is_better", "prior_value", "candidate_values"),
    [
        (True, 100.0, [100.0, 120.0, 110.0]),
        (False, 100.0, [100.0, 80.0, 90.0]),
    ],
)
def test_selector_keeps_incumbent_when_no_candidate_improves_objective(
    lower_is_better: bool,
    prior_value: float,
    candidate_values: list[float],
):
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": lower_is_better},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {
            "candidate": i,
            "sha": f"candidate-{i}",
            "gate_passed": True,
            "eligible": True,
            "metrics": {"OBJECTIVE": value, "CORRECTNESS": True},
        }
        for i, value in enumerate(candidate_values)
    ]

    assert _select_winner(
        candidates,
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    ) is None


@pytest.mark.parametrize(
    ("lower_is_better", "prior_value", "candidate_values", "winner"),
    [
        (True, 100.0, [110.0, 90.0, 95.0], "candidate-1"),
        (False, 100.0, [90.0, 105.0, 101.0], "candidate-1"),
    ],
)
def test_selector_advances_only_when_best_candidate_improves_objective(
    lower_is_better: bool,
    prior_value: float,
    candidate_values: list[float],
    winner: str,
):
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": lower_is_better},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {
            "candidate": i,
            "sha": f"candidate-{i}",
            "gate_passed": True,
            "eligible": True,
            "metrics": {"OBJECTIVE": value, "CORRECTNESS": True},
        }
        for i, value in enumerate(candidate_values)
    ]

    selected = _select_winner(
        candidates,
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    )

    assert selected is not None
    assert selected["sha"] == winner


def test_selector_uses_best_candidate_when_prior_objective_is_missing():
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 0, "sha": "slow", "gate_passed": True, "eligible": True,
         "metrics": {"OBJECTIVE": 20.0, "CORRECTNESS": True}},
        {"candidate": 1, "sha": "fast", "gate_passed": True, "eligible": True,
         "metrics": {"OBJECTIVE": 10.0, "CORRECTNESS": True}},
    ]

    assert _select_winner(candidates, schema, prior_metrics={})["sha"] == "fast"


def test_round_performance_reports_best_eligible_value_and_relative_improvement(
    capsys,
):
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 0, "sha": "valid", "gate_passed": True,
         "eligible": True,
         "metrics": {"OBJECTIVE": 110.0, "CORRECTNESS": True}},
        {"candidate": 1, "sha": "rejected", "gate_passed": False,
         "eligible": False,
         "metrics": {"OBJECTIVE": 50.0, "CORRECTNESS": False}},
    ]

    loop_mod._print_round_performance(
        round_id=2,
        candidates=candidates,
        metrics_schema=schema,
        prior_metrics={"OBJECTIVE": 100.0},
    )

    output = capsys.readouterr().out
    # Best eligible objective is reported; the rejected candidate is excluded;
    # an improvement indicator is shown. Assert behavior, not exact format.
    assert "harness performance round" in output
    assert "OBJECTIVE" in output
    assert "110" in output
    assert "OBJECTIVE=50" not in output
    assert "improvement" in output.lower()


def test_store_records_generation_candidates_and_proposer_view(tmp_path: Path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    })
    candidates = [
        {"candidate": 0, "proposal": "p0", "parent_sha": "base", "sha": "a",
         "status": "COMPLETED", "gate_passed": True, "eligible": True,
         "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
         "metrics": {"SPEED_MS": 600.0, "CORRECTNESS": True},
         "changed_paths": ["a.cc"], "selected": False},
        {"candidate": 1, "proposal": "p1", "parent_sha": "base", "sha": "b",
         "status": "COMPLETED", "gate_passed": True, "eligible": True,
         "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True},
         "changed_paths": ["b.cc"], "selected": True},
    ]
    store.append_generation(0, parent_sha="base", selected_candidate=1,
                            selected_sha="b", candidates=candidates)
    rows = store.history()
    assert rows[0]["selected_sha"] == "b"
    assert rows[0]["candidates"][1]["selected"] is True
    assert rows[0]["candidates"][0]["gates"]["CORRECTNESS"]["passed"] is True
    assert best_candidate(rows, store.metrics_schema)["sha"] == "b"


def test_store_keeps_parent_and_best_when_generation_has_no_winner(tmp_path: Path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    })
    winner = {
        "candidate": 0, "proposal": "p0", "sha": "best",
        "status": "COMPLETED", "gate_passed": True, "eligible": True,
        "metrics": {"SPEED_MS": 100.0, "CORRECTNESS": True},
        "selected": True,
    }
    store.append_generation(
        0,
        parent_sha="baseline",
        selected_candidate=0,
        selected_sha="best",
        candidates=[winner],
    )
    regressed = {
        "candidate": 0, "proposal": "p1", "sha": "slower",
        "status": "COMPLETED", "gate_passed": True, "eligible": True,
        "metrics": {"SPEED_MS": 120.0, "CORRECTNESS": True},
        "selected": False,
    }
    store.append_generation(
        1,
        parent_sha="best",
        selected_candidate=None,
        selected_sha=None,
        candidates=[regressed],
    )

    generation = store.history()[1]
    assert generation["selected_candidate"] is None
    assert generation["selected_sha"] is None
    assert generation["base_sha"] == "best"
    assert generation["candidates"][0]["sha"] == "slower"
    assert generation["candidates"][0]["selected"] is False
    assert best_candidate(
        store.history(), store.metrics_schema,
    )["sha"] == "best"


def test_run_candidates_uses_same_parent_for_all_worktrees(monkeypatch, tmp_path: Path):
    class FakeWorkspace:
        def __init__(self):
            self.added = []
            self.removed = []

        def add_worktree(self, round_id, parent_sha):
            self.added.append((round_id, parent_sha))
            return tmp_path / str(round_id)

        def remove_worktree(self, round_id):
            self.removed.append(round_id)

        def diff(self, parent_sha, sha):
            return f"diff {parent_sha}..{sha}"

    def fake_execute(agent, *, proposal, goal, editable, frozen, workspace, worktree, round_id, gate_block="", prompt_dir=None):
        return ExecResult(
            sha=f"sha-{round_id}", reason=None,
            changed_paths=[f"{round_id}.cc"], path_gate_passed=True,
            path_gate_violations=[])

    def fake_run_eval(commands, cwd, runtime, metrics_schema=None, **kwargs):
        cid = int(str(cwd).rsplit("c", 1)[-1])
        return EvalResult(
            "eval",
            {"SPEED_MS": 100.0 + cid, "CORRECTNESS": True},
            (0,),
        )

    monkeypatch.setattr(worker_mod.executor_mod, "execute", fake_execute)
    monkeypatch.setattr(worker_mod.evals, "run_eval", fake_run_eval)

    workspace = FakeWorkspace()
    proposals = ["p0", "p1", "p2"]
    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
              "gates": [{"key": "CORRECTNESS"}]}
    ctx = RunContext(
        cfg={
            "goal": "g", "editable_paths": ["src/**"], "frozen_paths": [],
            "eval_commands": ["eval"], "max_workers": 1, "metrics": schema,
        },
        workspace=workspace, executor_agent=object(),
        runtime=object(), baseline_metrics={"SPEED_MS": 200.0},
    )
    candidates = _run_candidates(ctx, proposals, 7, "parent")

    assert workspace.added == [("7-c0", "parent"), ("7-c1", "parent"), ("7-c2", "parent")]
    assert workspace.removed == ["7-c0", "7-c1", "7-c2"]
    assert [candidate["proposal"] for candidate in candidates] == proposals
    assert _select_winner(candidates, schema)["candidate"] == 0


def test_run_candidates_logs_candidate_local_failure(monkeypatch, tmp_path: Path, capsys):
    class FakeWorkspace:
        def add_worktree(self, round_id, parent_sha):
            return tmp_path / str(round_id)

        def remove_worktree(self, round_id):
            pass

        def diff(self, parent_sha, sha):
            return "diff"

    def fake_execute(*_args, **_kwargs):
        return ExecResult(
            sha="candidate", reason=None, changed_paths=["a.cc"],
            path_gate_passed=True, path_gate_violations=[])

    monkeypatch.setattr(worker_mod.executor_mod, "execute", fake_execute)
    monkeypatch.setattr(
        worker_mod.evals, "run_eval",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("eval exploded")),
    )

    ctx = RunContext(
        cfg={
            "goal": "g", "editable_paths": ["src/**"], "frozen_paths": [],
            "eval_commands": [], "max_workers": 1, "metrics": _SCHEMA,
        },
        workspace=FakeWorkspace(), executor_agent=object(),
        runtime=object(),
    )
    candidates = _run_candidates(ctx, ["p0"], 2, "parent")

    assert candidates[0]["status"] == "EVAL_FAILED"
    assert candidates[0]["eligible"] is False
    assert "eval exploded" in candidates[0]["eval_block"]
    assert "candidate r2-c0 eval error:" in capsys.readouterr().out


def test_run_candidates_logs_outer_parallel_worker_failure(monkeypatch, capsys):
    def fail_worker(_ctx, candidate_id, *_args, **_kwargs):
        raise RuntimeError(f"worker {candidate_id} exploded")

    monkeypatch.setattr(loop_mod, "_run_one_candidate", fail_worker)
    candidates = _run_candidates(
        RunContext(cfg={"max_workers": 2}),
        ["p0", "p1"],
        3,
        "parent",
    )

    assert [candidate["status"] for candidate in candidates] == [
        "WORKER_FAILED", "WORKER_FAILED",
    ]
    out = capsys.readouterr().out
    assert "candidate r3-c0 worker failed: worker 0 exploded" in out
    assert "candidate r3-c1 worker failed: worker 1 exploded" in out


def test_run_candidates_normalizes_serial_worker_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        loop_mod, "_run_one_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("serial worker exploded")
        ),
    )

    candidates = _run_candidates(
        RunContext(cfg={"max_workers": 1}), ["p0"], 4, "parent",
    )

    assert candidates[0]["status"] == "WORKER_FAILED"
    assert candidates[0]["parent_sha"] == "parent"
    assert "candidate r4-c0 worker failed: serial worker exploded" in (
        capsys.readouterr().out
    )


def test_agent_structured_json_uses_validated_output(monkeypatch, tmp_path: Path):
    agent = Agent(runtime=object())
    expected = {"proposals": []}

    def fake_run(*_args, **_kwargs):
        return AgentResult(text="ignored", data=expected)

    monkeypatch.setattr(agent, "_run", fake_run)

    assert agent.run_json(
        "prompt",
        cwd=tmp_path,
        label="proposer",
        json_schema={"type": "object"},
    ) is expected


def test_agent_structured_json_rejects_prose_wrapped_json(monkeypatch, tmp_path: Path):
    agent = Agent(runtime=object())

    def fake_run(*_args, **_kwargs):
        return AgentResult(
            text='explanation before {"proposals":[]}',
            data={},
        )

    monkeypatch.setattr(agent, "_run", fake_run)

    with pytest.raises(AgentError, match="not an exact JSON object"):
        agent.run_json(
            "prompt",
            cwd=tmp_path,
            label="proposer",
            json_schema={"type": "object"},
        )


def test_next_proposals_uses_readonly_parent_snapshot_and_memory_service(tmp_path):

    class FakeWorkspace:
        repo = tmp_path / "repo"
        calls = []

        def add_worktree(self, worktree_id, parent_sha):
            self.calls.append(("add", worktree_id, parent_sha))
            path = tmp_path / "snapshot"
            path.mkdir(exist_ok=True)
            return path

        def remove_worktree(self, worktree_id):
            self.calls.append(("remove", worktree_id))

    class FakeProposer:
        kwargs = None

        def run(self, **kwargs):
            self.kwargs = kwargs
            proposal = ResearchProposal(
                instruction="try cache",
                research_target=NewFindingTarget(question="cache?"),
            )
            return ProposerResult([proposal])

    proposer = FakeProposer()
    ctx = RunContext(
        cfg={
            "goal": "faster", "editable_paths": ["src/**"],
            "frozen_paths": [], "candidates_per_round": 1,
        },
        run_dir=tmp_path,
        workspace=FakeWorkspace(),
        store=None,
        proposer_agent=proposer,
        memory_service=MemoryService(tmp_path, metrics_schema=_SCHEMA),
    )

    result = loop_mod._next_proposals(ctx, None, 1, "parent-sha")

    assert result.proposals[0].instruction == "try cache"
    assert proposer.kwargs["memory_service"] is ctx.memory_service
    assert proposer.kwargs["source_path"] == tmp_path / "snapshot"
    assert proposer.kwargs["current_round"] == 1
    assert ctx.workspace.calls == [
        ("add", "proposer-1", "parent-sha"),
        ("remove", "proposer-1"),
    ]


def test_next_proposals_removes_snapshot_when_proposer_fails(tmp_path):
    removed = []

    class FakeWorkspace:
        repo = tmp_path / "repo"

        def add_worktree(self, worktree_id, _parent_sha):
            path = tmp_path / "snapshot"
            path.mkdir(exist_ok=True)
            return path

        def remove_worktree(self, worktree_id):
            removed.append(worktree_id)

    class FailingProposer:
        def run(self, **_kwargs):
            raise ValueError("bad proposal")

    ctx = RunContext(
        cfg={"goal": "faster", "editable_paths": [], "frozen_paths": []},
        run_dir=tmp_path, workspace=FakeWorkspace(),
        store=type("Store", (), {"history": lambda self: []})(),
        proposer_agent=FailingProposer(),
        memory_service=MemoryService(tmp_path, metrics_schema=_SCHEMA),
    )

    with pytest.raises(ValueError, match="bad proposal"):
        loop_mod._next_proposals(ctx, None, 2, "parent")

    assert removed == ["proposer-2"]


def test_next_proposals_static_mode_wraps_instruction_as_new_target(tmp_path):
    result = loop_mod._next_proposals(
        RunContext(cfg={}), ["fixed"], 0, "parent",
    )

    assert len(result.proposals) == 1
    assert result.proposals[0].instruction == "fixed"
    assert isinstance(
        result.proposals[0].research_target, NewFindingTarget,
    )

def _run_loop_integration(
    monkeypatch, tmp_path, *, prompt_dir=None, max_rounds=2,
    target_rounds=None,
):
    run_dir = tmp_path / "run"
    seed = Store(run_dir, metrics_schema=_SCHEMA)
    seed.append_generation(
        0,
        parent_sha="baseline-sha",
        selected_candidate=0,
        selected_sha="seed-sha",
        candidates=[{
            "candidate": 0,
            "proposal": "seed proposal",
            "parent_sha": "baseline-sha",
            "sha": "seed-sha",
            "status": "COMPLETED",
            "gate_passed": True,
            "eligible": True,
            "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
            "metrics": {"SPEED_MS": 100.0, "CORRECTNESS": True},
        }],
    )
    cfg = {
        "goal": "go faster",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "max_rounds": max_rounds,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "metrics": _SCHEMA,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "eval_commands": [],
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [],
        "roles": {
            "researcher": {
                "model": "gpt-5.5", "base_url": "https://example.invalid",
                "max_steps": 5, "command_timeout_seconds": 2,
                "command_output_cap_chars": 1000,
            },
            "executor": {
                "model": "glm-5", "base_url": "https://example.invalid",
            },
        },
    }

    class FakeRuntime:
        def __init__(self, **_kwargs):
            pass

        def summary_lines(self):
            return ()

        def preflight(self):
            pass

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.runtime = object()

    class FakeModel:
        @classmethod
        def from_config(cls, _config):
            return object()

    class FakeProposer:
        def __init__(self, **_kwargs):
            pass

        def run(self, **kwargs):
            assert kwargs["current_round"] == 1
            assert kwargs["prompt_dir"] == prompt_dir
            return ProposerResult(
                [ResearchProposal(
                    instruction="test another sparse gather",
                    research_target=NewFindingTarget(
                        question="Does sparse gather still dominate?",
                    ),
                )],
            )

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.run_dir = run_dir
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline-sha"

        def add_worktree(self, worktree_id, _parent_sha):
            worktree = self.run_dir / "worktrees" / f"r{worktree_id}"
            worktree.mkdir(parents=True, exist_ok=True)
            return worktree

        def remove_worktree(self, _worktree_id):
            pass

    class FakeBackend:
        def __init__(self, ctx):
            pass

        def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
            return "", {}

        def run_candidates(self, *, proposals: list[str], round_id: int,
                           parent_sha: str, journal=None,
                           finding_ids=None) -> list[dict]:
            assert proposals == ["test another sparse gather"]
            # The inflight journal carries structured proposal metadata,
            # NOT annotations.
            assert "annotations" not in journal.meta
            assert journal.meta["proposals"] == [
                {"instruction": "test another sparse gather",
                 "finding_id": "F-001",
                 "evidence_refs": [],
                 "material_difference": None},
            ]
            assert finding_ids == ["F-001"]
            return fake_run_candidates()

        def resume_round(self, jobs: list[dict], *, round_id: int,
                         parent_sha: str, journal=None,
                         finding_ids=None) -> list[dict]:
            return []

    executed = []

    def fake_run_candidates(*_args, **_kwargs):
        executed.append(True)
        return [{
            "candidate": 0,
            "experiment_id": "r1c0",
            "finding_id": "F-001",
            "proposal": "test another sparse gather",
            "parent_sha": "seed-sha",
            "sha": None,
            "status": "NO_CHANGE",
            "metrics": {},
            "changed_paths": [],
            "gates": {},
            "gate_passed": False,
            "eligible": False,
            "selected": False,
        }]

    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod.model_mod, "HepAIChatModel", FakeModel)
    monkeypatch.setattr(loop_mod.proposer_mod, "ProposerAgent", FakeProposer)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "build_backend", lambda ctx: FakeBackend(ctx))
    monkeypatch.setattr(loop_mod, "_select_winner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop_mod, "_refresh_progress_plot", lambda *_args: None)

    loop_mod.run(
        "config.yaml", run_dir, continue_run=True, prompt_dir=prompt_dir,
        target_rounds=target_rounds,
    )
    return run_dir, executed


def test_run_records_experiment_and_finding_but_no_notes(
    monkeypatch, tmp_path,
):
    run_dir, _ = _run_loop_integration(
        monkeypatch,
        tmp_path,
        prompt_dir=tmp_path / "active-prompts",
    )
    store = Store(run_dir, metrics_schema=_SCHEMA)
    rounds = store.history()
    r1_candidate = rounds[-1]["candidates"][0]
    # New candidate schema: no `note`; experiment_id and finding_id are set.
    assert "note" not in r1_candidate
    assert r1_candidate["experiment_id"] == "r1c0"
    assert r1_candidate["finding_id"] == "F-001"
    # The Findings archive gained an entry (state=active after linking).
    findings_path = run_dir / "memory" / "findings.jsonl"
    assert findings_path.is_file()


def test_segment_progress_reports_global_total_and_next_optimizer(
    monkeypatch, tmp_path, capsys,
):
    _run_loop_integration(
        monkeypatch,
        tmp_path,
        max_rounds=6,
        target_rounds=2,
    )

    output = capsys.readouterr().out
    # Global round framing and the optimizer-boundary marker are shown —
    # assert content, not the exact framing strings.
    assert "current round 2/6" in output
    assert "optimizer" in output.lower()
    assert "after round 2" in output


def test_run_aborts_before_executor_when_proposer_contract_fails(
    monkeypatch, tmp_path: Path
):
    cfg = {
        "goal": "go faster",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "max_rounds": 1,
        "candidates_per_round": 3,
        "max_workers": 3,
        "agent_timeout_seconds": 10,
        "metrics": None,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "eval_commands": [],
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [],
        "roles": {
            "researcher": {
                "model": "gpt-5.5", "base_url": "https://example.invalid",
                "max_steps": 5, "command_timeout_seconds": 2,
                "command_output_cap_chars": 1000,
            },
            "executor": {
                "model": "glm-5", "base_url": "https://example.invalid",
            },
        },
    }

    class FakeRuntime:
        def __init__(self, **_kwargs):
            pass

        def summary_lines(self):
            return ()

        def preflight(self):
            pass

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

    class FakeModel:
        @classmethod
        def from_config(cls, _config):
            return object()

    class FailingProposer:
        def __init__(self, **_kwargs):
            pass

        def run(self, **_kwargs):
            raise ValueError("invalid proposer batch")

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.run_dir = run_dir
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline-sha"

        def add_worktree(self, worktree_id, _parent_sha):
            worktree = self.run_dir / "worktrees" / f"r{worktree_id}"
            worktree.mkdir(parents=True, exist_ok=True)
            return worktree

        def remove_worktree(self, _worktree_id):
            pass

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def history(self):
            return []

        def append(self, *_args, **_kwargs):
            raise AssertionError("proposer failure must not be written as a round")

    executor_called = False

    def fail_if_executor_runs(*_args, **_kwargs):
        nonlocal executor_called
        executor_called = True
        raise AssertionError("executor must not run")

    class FakeBackend:
        def __init__(self, ctx):
            pass

        def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
            return "", {}

        def run_candidates(self, *, proposals: list[str], round_id: int,
                           parent_sha: str, journal=None,
                           finding_ids=None) -> list[dict]:
            return fail_if_executor_runs()

        def resume_round(self, jobs: list[dict], *, round_id: int,
                         parent_sha: str, journal=None,
                         finding_ids=None) -> list[dict]:
            return []

    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod.model_mod, "HepAIChatModel", FakeModel)
    monkeypatch.setattr(loop_mod.proposer_mod, "ProposerAgent", FailingProposer)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "Store", FakeStore)
    monkeypatch.setattr(loop_mod, "build_backend", lambda ctx: FakeBackend(ctx))

    with pytest.raises(ValueError, match="invalid proposer batch"):
        loop_mod.run("config.yaml", tmp_path / "run")

    assert executor_called is False
