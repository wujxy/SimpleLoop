from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from simpleloop import candidate_worker as worker_mod
from simpleloop import config as config_mod
from simpleloop import loop as loop_mod
from simpleloop.candidate import (
    CandidateArtifact,
    CandidateBatchRequest,
    CandidatePlan,
    CandidateResult,
    CandidateStatus,
    EvaluationResult,
    ExecutionResult,
    GateDecision,
)
from proposer.memory import MemoryService
from simpleloop.roles.agent import Agent, AgentError, AgentResult
from simpleloop.loop import RunContext
from simpleloop.execution import proposer_lanes
from simpleloop.execution.base import InfraRoundError
from simpleloop.execution.local import LocalBackend
from simpleloop.harness.store import Store, best_candidate
from simpleloop.stages.proposer import Proposal, ProposalBatch, ProposerRequest
from simpleloop.stages.selector import select_candidate
from simpleloop.world import ProcessResult, SourceWorkspace
from round_helpers import append_round


EXAMPLES = Path(__file__).parents[1] / "examples"

_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
           "gates": [{"key": "CORRECTNESS"}]}


def _typed(rows: list[dict]) -> tuple[CandidateResult, ...]:
    """Translate compact policy-test facts into the production contract."""
    results = []
    for row in rows:
        candidate_id = int(row.get("candidate", 0))
        parent_sha = str(row.get("parent_sha", "parent"))
        sha = row.get("sha")
        status = CandidateStatus(row.get(
            "status",
            "COMPLETED" if row.get("gate_passed") else "GATE_REJECTED",
        ))
        metrics = row.get("metrics") or {}
        results.append(CandidateResult(
            candidate_id=candidate_id,
            experiment_id=str(row.get("experiment_id", f"r0c{candidate_id}")),
            proposal=Proposal(str(row.get("proposal", f"p{candidate_id}"))),
            parent_sha=parent_sha,
            status=status,
            execution=ExecutionResult(status.value),
            artifact=(
                CandidateArtifact(parent_sha, str(sha)) if sha else None
            ),
            evaluation=(
                EvaluationResult("", metrics) if metrics else None
            ),
            gate=GateDecision(
                {}, bool(row.get("gate_passed")), bool(row.get("eligible")),
            ),
            usage=tuple(row.get("usage") or ()),
            telemetry=row.get("telemetry") or {},
        ))
    return tuple(results)


def _select(candidates, schema, prior_metrics=None):
    objective = schema["objective"]
    prior = (prior_metrics or {}).get(objective["key"])
    return select_candidate(
        candidates=tuple(candidates),
        objective_key=objective["key"],
        lower_is_better=objective["lower_is_better"],
        incumbent_value=(
            float(prior)
            if isinstance(prior, (int, float)) and not isinstance(prior, bool)
            else None
        ),
    )


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
        "safety": {"editable_paths": ["src/**"]},
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
    assert "OMILRECV2/src" in raw["safety"]["editable_paths"]
    assert "OMILRECV2/CMakeLists.txt" in raw["safety"]["editable_paths"]
    # frozen_paths AND read_only_paths are both gone — the read-only world is
    # the whole worktree minus editable, enforced by the mount (ro base + :rw
    # overlay for editable), not by an explicit frozen list.
    assert "frozen_paths" not in raw["safety"]
    assert "read_only_paths" not in raw["safety"]


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
    assert _select(_typed(candidates), schema).sha == "winner"


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
    assert _select(_typed(candidates), schema).sha == "a"


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

    assert _select(
        _typed(candidates),
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    ).sha is None


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

    selected = _select(
        _typed(candidates),
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    )

    assert selected.sha is not None
    assert selected.sha == winner


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

    assert _select(
        _typed(candidates), schema, prior_metrics={}
    ).sha == "fast"


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
        candidates=_typed(candidates),
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
    append_round(store, 0, parent_sha="base", selected_candidate=1,
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
    append_round(store,
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
    append_round(store,
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


def test_local_batch_uses_each_plan_parent_and_preserves_order(tmp_path: Path):
    class FakeWorkspace:
        def __init__(self):
            self.added = []
            self.removed = []

        def create(self, spec):
            self.added.append((spec.workspace_id, spec.revision))
            path = tmp_path / spec.workspace_id
            path.mkdir(exist_ok=True)
            return SourceWorkspace(spec.workspace_id, path, spec.revision)

        def remove(self, workspace):
            self.removed.append(workspace.workspace_id)

    workspace = FakeWorkspace()
    seen = []

    def candidate_runner(request):
        seen.append(request)
        return _typed([{
            "candidate": request.candidate_id,
            "parent_sha": request.parent_sha,
            "proposal": request.proposal.instruction,
            "sha": f"sha-{request.candidate_id}",
            "status": "COMPLETED",
            "metrics": {"SPEED_MS": 100.0 + request.candidate_id},
            "gate_passed": True,
            "eligible": True,
        }])[0]

    ctx = RunContext(
        cfg={"max_workers": 1},
        workspace=workspace,
    )
    plans = (
        CandidatePlan(0, "parent-a", Proposal("p0")),
        CandidatePlan(1, "parent-b", Proposal("p1")),
    )

    candidates = LocalBackend(
        ctx, candidate_runner=candidate_runner,
    ).run_candidates(CandidateBatchRequest(7, plans))

    assert workspace.added == [
        ("7-c0", "parent-a"), ("7-c1", "parent-b"),
    ]
    assert workspace.removed == ["7-c0", "7-c1"]
    assert [request.parent_sha for request in seen] == [
        "parent-a", "parent-b",
    ]
    assert [candidate.candidate_id for candidate in candidates] == [0, 1]


def test_local_parallel_normalizes_each_runner_failure(tmp_path: Path, capsys):
    class FakeWorkspace:
        def create(self, spec):
            path = tmp_path / spec.workspace_id
            path.mkdir(exist_ok=True)
            return SourceWorkspace(spec.workspace_id, path, spec.revision)

        def remove(self, workspace):
            pass

    def fail_worker(request):
        raise RuntimeError(f"worker {request.candidate_id} exploded")

    candidates = LocalBackend(
        RunContext(cfg={"max_workers": 2}, workspace=FakeWorkspace()),
        candidate_runner=fail_worker,
    ).run_candidates(CandidateBatchRequest(
        3,
        (
            CandidatePlan(0, "parent", Proposal("p0")),
            CandidatePlan(1, "parent", Proposal("p1")),
        ),
    ))

    assert [candidate.status for candidate in candidates] == [
        CandidateStatus.WORKER_FAILED, CandidateStatus.WORKER_FAILED,
    ]
    out = capsys.readouterr().out
    assert "candidate r3-c0 worker failed: worker 0 exploded" in out
    assert "candidate r3-c1 worker failed: worker 1 exploded" in out


def test_local_serial_normalizes_runner_failure(tmp_path: Path, capsys):
    class FakeWorkspace:
        def create(self, spec):
            path = tmp_path / spec.workspace_id
            path.mkdir(exist_ok=True)
            return SourceWorkspace(spec.workspace_id, path, spec.revision)

        def remove(self, workspace):
            pass

    def fail_worker(request):
        raise RuntimeError("serial worker exploded")

    candidates = LocalBackend(
        RunContext(cfg={"max_workers": 1}, workspace=FakeWorkspace()),
        candidate_runner=fail_worker,
    ).run_candidates(CandidateBatchRequest(
        4,
        (CandidatePlan(0, "parent", Proposal("p0")),),
    ))

    assert candidates[0].status is CandidateStatus.WORKER_FAILED
    assert candidates[0].parent_sha == "parent"
    assert "candidate r4-c0 worker failed: serial worker exploded" in (
        capsys.readouterr().out
    )


def test_agent_structured_json_uses_validated_output(monkeypatch, tmp_path: Path):
    agent = Agent(world=object())
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
    agent = Agent(world=object())

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


def test_next_proposals_creates_lane_workspaces_and_passes_them(tmp_path):

    class FakeWorkspace:
        repo = tmp_path / "repo"
        calls = []

        def create_lane(self, lane_id, base_sha):
            self.calls.append(("add", lane_id, base_sha))
            path = tmp_path / f"lane-{lane_id}"
            path.mkdir(exist_ok=True)
            return SourceWorkspace(f"lane-{lane_id}", path, base_sha)

        def remove_lane(self, workspace):
            self.calls.append(("remove", int(workspace.workspace_id[5:])))

    manifest_seen = {}

    def fake_lane_runner(spec, result_dir):
        # S2a: the lane_runner replaces the in-process proposer call. The
        # manifest has already been written by run_proposer_lanes — verify it
        # threads the round context into what the worker subprocess would read.
        manifest_seen.update(json.loads(
            (Path(result_dir) / "manifest.json").read_text()))
        return proposer_lanes.LaneJob(
            lane_id=0, result_dir=Path(result_dir), state="COMPLETED",
            result={"status": "COMPLETED", "outcome": "submit",
                    "proposals": [{"instruction": "try cache",
                                   "research_target": {"question": "cache?"},
                                   "evidence_refs": [],
                                   "material_difference": None}],
                    "reason_kind": None, "explanation": "",
                    "abstain_reason": None, "trace": {}, "telemetry": {}})

    ctx = RunContext(
        cfg={
            "goal": "faster", "editable_paths": ["src/**"],
            "candidates_per_round": 1,
        },
        run_dir=tmp_path,
        workspace=FakeWorkspace(),
        store=None,
    )

    result = LocalBackend(ctx, lane_runner=fake_lane_runner).run_proposer_lanes(
        ProposerRequest(1, "faster", "parent-sha"))

    assert result.proposals[0].instruction == "try cache"
    # candidates_per_round=1 -> proposal_slots=1; the workspace path + round
    # are threaded into the worker via the manifest.
    assert manifest_seen["proposal_slots"] == 1
    assert manifest_seen["workspace_path"] == str(tmp_path / "lane-0")
    assert manifest_seen["round_id"] == 1
    # add_lane_workspace before spawn, remove_lane_workspace in the finally.
    assert ctx.workspace.calls == [
        ("add", 0, "parent-sha"),
        ("remove", 0),
    ]


def test_next_proposals_removes_lane_workspaces_when_proposer_fails(tmp_path):
    removed = []

    class FakeWorkspace:
        repo = tmp_path / "repo"

        def create_lane(self, lane_id, base_sha):
            path = tmp_path / f"lane-{lane_id}"
            path.mkdir(exist_ok=True)
            return SourceWorkspace(f"lane-{lane_id}", path, base_sha)

        def remove_lane(self, workspace):
            removed.append(int(workspace.workspace_id[5:]))

    def failing_lane_runner(_spec, _result_dir):
        # S2a: an infrastructure failure (worker killed, no _FINISHED) surfaces
        # as InfraRoundError from the lane runner.
        raise InfraRoundError("worker exited without _FINISHED")

    ctx = RunContext(
        cfg={"goal": "faster", "editable_paths": [],
             "candidates_per_round": 1},
        run_dir=tmp_path, workspace=FakeWorkspace(),
        store=type("Store", (), {"history": lambda self: []})(),
    )

    with pytest.raises(InfraRoundError, match="without _FINISHED"):
        LocalBackend(ctx, lane_runner=failing_lane_runner).run_proposer_lanes(
            ProposerRequest(2, "faster", "parent"))

    # The finally still tears down the lane workspace on infra failure.
    assert removed == [0]


def test_next_proposals_static_mode_returns_host_proposal(tmp_path):
    result = loop_mod._next_proposals(
        RunContext(cfg={}), ["fixed"], 0, "parent",
    )

    assert len(result.proposals) == 1
    assert result.proposals[0].instruction == "fixed"
    assert not hasattr(result.proposals[0], "research_target")

def _run_loop_integration(
    monkeypatch, tmp_path, *, prompt_dir=None, max_rounds=2,
    target_rounds=None,
):
    run_dir = tmp_path / "run"
    seed = Store(run_dir, metrics_schema=_SCHEMA)
    append_round(seed,
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
        "read_only_binds": [tmp_path / "external-data"],
        "roles": {
            "researcher": {
                "model": "gpt-5.5", "base_url": "https://example.invalid",
                "command_timeout_seconds": 2,
                "command_output_cap_chars": 1000,
            },
            "executor": {
                "model": "glm-5", "base_url": "https://example.invalid",
            },
        },
    }

    class FakeSandbox:
        preflight_calls = []

        def preflight(self, spec):
            self.preflight_calls.append(spec)

    class FakeWorld:
        def run(self, request):
            return ProcessResult(request.argv, 0, "", "", 0.1)

    class FakeWorldBuilder:
        builds = []

        def __init__(self, _sandbox):
            pass

        def build(self, workspace, sandbox, world):
            self.builds.append((workspace, sandbox, world))
            return FakeWorld()

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.runtime = object()

    class FakeWorkspace:
        def __init__(self, run_dir, *_args, **_kwargs):
            self.run_dir = Path(run_dir)
            self.repo = self.run_dir / "repo"

        def initialize(self):
            self.repo.mkdir(parents=True, exist_ok=True)
            return "baseline-sha"

        def baseline_sha(self):
            return "baseline-sha"

        def create(self, spec):
            worktree = self.run_dir / "worktrees" / f"r{spec.workspace_id}"
            worktree.mkdir(parents=True, exist_ok=True)
            return SourceWorkspace(spec.workspace_id, worktree, spec.revision)

        def remove(self, _workspace):
            pass

        def add_lane_workspace(self, lane_id, _base_sha):
            ws = self.run_dir / "lanes" / f"lane-{lane_id}" / "workspace"
            ws.mkdir(parents=True, exist_ok=True)
            return ws

        def remove_lane_workspace(self, _lane_id):
            pass

    class FakeBackend:
        def __init__(self, ctx):
            self.ctx = ctx

        def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
            return "", {}

        def run_proposer_lanes(self, request):
            # S2a: LOCAL runs the proposer as a subprocess, so an in-process
            # proposer is invisible to it. Return the Host batch directly —
            # this test exercises proposal -> executor -> record flow.
            assert request.incumbent_sha == "seed-sha"
            return ProposalBatch((Proposal("test another sparse gather"),))

        def cleanup_proposer_orphans(self):
            pass

        def run_candidates(self, request, *, journal=None) -> tuple[CandidateResult, ...]:
            assert [
                plan.proposal.instruction for plan in request.candidates
            ] == ["test another sparse gather"]
            assert request.candidates[0].parent_sha == "seed-sha"
            # The inflight journal carries structured proposal metadata, NOT
            # annotations. finding_id is no longer threaded through the
            # execution layer — the proposer owns the finding lifecycle
            # internally (commit_proposals), and the Kernel ledger carries no
            # finding semantics.
            assert "annotations" not in journal.meta
            assert journal.meta["proposals"] == [
                {"instruction": "test another sparse gather",
                 "evidence_refs": []},
            ]
            return fake_run_candidates()

        def resume_round(self, jobs: list[dict], *, round_id: int,
                         parent_sha: str, journal=None) -> tuple[CandidateResult, ...]:
            return ()

    executed = []

    def fake_run_candidates(*_args, **_kwargs):
        executed.append(True)
        return _typed([{
            "candidate": 0,
            "experiment_id": "r1c0",
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
        }])

    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "ApptainerSandbox", FakeSandbox)
    monkeypatch.setattr(loop_mod, "WorldBuilder", FakeWorldBuilder)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod, "GitWorkspaceProvider", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "build_backend", lambda ctx: FakeBackend(ctx))
    monkeypatch.setattr(loop_mod, "_refresh_progress_plot", lambda *_args: None)

    loop_mod.run(
        "config.yaml", run_dir, continue_run=True, prompt_dir=prompt_dir,
        target_rounds=target_rounds,
    )
    assert len(FakeSandbox.preflight_calls) == 1
    preflight_world = FakeWorldBuilder.builds[0][2]
    assert preflight_world.external_mounts[0].source == tmp_path / "external-data"
    return run_dir, executed


def test_run_records_experiment_no_finding_in_kernel(
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
    # Candidate schema: no `note`, experiment_id is set, and the Kernel ledger
    # carries NO finding_id (S2c: finding↔experiment attribution is the
    # proposer's own concern, re-derived at read time — never stored in
    # history.jsonl).
    assert "note" not in r1_candidate
    assert r1_candidate["experiment_id"] == "r1c0"
    assert "finding_id" not in r1_candidate


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
                "command_timeout_seconds": 2,
                "command_output_cap_chars": 1000,
            },
            "executor": {
                "model": "glm-5", "base_url": "https://example.invalid",
            },
        },
    }

    class FakeSandbox:
        def preflight(self, _spec):
            pass

    class FakeWorld:
        def run(self, request):
            return ProcessResult(request.argv, 0, "", "", 0.1)

    class FakeWorldBuilder:
        def __init__(self, _sandbox):
            pass

        def build(self, _workspace, _sandbox, _world):
            return FakeWorld()

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
        def __init__(self, run_dir, *_args, **_kwargs):
            self.run_dir = Path(run_dir)
            self.repo = self.run_dir / "repo"

        def initialize(self):
            self.repo.mkdir(parents=True, exist_ok=True)
            return "baseline-sha"

        def baseline_sha(self):
            return "baseline-sha"

        def create(self, spec):
            worktree = self.run_dir / "worktrees" / f"r{spec.workspace_id}"
            worktree.mkdir(parents=True, exist_ok=True)
            return SourceWorkspace(spec.workspace_id, worktree, spec.revision)

        def remove(self, _workspace):
            pass

        def add_lane_workspace(self, lane_id, _base_sha):
            ws = self.run_dir / "lanes" / f"lane-{lane_id}" / "workspace"
            ws.mkdir(parents=True, exist_ok=True)
            return ws

        def remove_lane_workspace(self, _lane_id):
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
            self.ctx = ctx

        def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
            return "", {}

        def run_proposer_lanes(self, request):
            # S2a: simulate a proposer contract failure directly (a subprocess
            # can't see an in-process faked ProposerOrchestrator).
            assert request.incumbent_sha == "baseline-sha"
            raise ValueError("invalid proposer batch")

        def cleanup_proposer_orphans(self):
            pass

        def run_candidates(self, request, *, journal=None) -> list[dict]:
            return fail_if_executor_runs()

        def resume_round(self, jobs: list[dict], *, round_id: int,
                         parent_sha: str, journal=None,
                         finding_ids=None) -> list[dict]:
            return []

    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "ApptainerSandbox", FakeSandbox)
    monkeypatch.setattr(loop_mod, "WorldBuilder", FakeWorldBuilder)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod, "GitWorkspaceProvider", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "Store", FakeStore)
    monkeypatch.setattr(loop_mod, "build_backend", lambda ctx: FakeBackend(ctx))

    with pytest.raises(ValueError, match="invalid proposer batch"):
        loop_mod.run("config.yaml", tmp_path / "run")

    assert executor_called is False
