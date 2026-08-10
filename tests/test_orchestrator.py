from pathlib import Path
import random

from simpleloop.memory.models import NewFindingTarget, ResearchProposal
from simpleloop.roles.orchestrator import ProposerOrchestrator, _lane_quotas, _sample_generative_ops
from simpleloop.roles.proposer import ScientistResult


def _proposal(name):
    return ResearchProposal(
        instruction=name,
        research_target=NewFindingTarget(question=f"question {name}"),
        model_claim_refs=("M1",), explanation_refs=("E1",),
        hypothesis_id="H1", evidence_refs=("source:src/a.cc",),
        mechanism="reuse", prediction="lower repeated work",
        affected_scope="src/a.cc",
    )


def test_lane_quotas_cover_requested_candidates_without_padding():
    assert _lane_quotas(4, 2) == [2, 2]
    assert _lane_quotas(5, 2) == [2, 2, 1]
    assert _lane_quotas(0, 2) == []


def test_generative_sampling_is_seeded_and_distinct():
    first = _sample_generative_ops(random.Random(7))
    second = _sample_generative_ops(random.Random(7))
    assert first == second
    assert len(first) == len(set(first)) == 5


class _FakeScientist:
    def __init__(self):
        self.calls = []

    def run_lane(self, **kwargs):
        self.calls.append(kwargs)
        return ScientistResult(
            proposals=tuple(
                _proposal(f"agent-{id(self)}-{index}")
                for index in range(kwargs["select_quota"])
            ),
            outcome="proposals",
            deliberation_telemetry={"steps": 8},
            trace={"current_phase": "deepen", "outcome": "proposals"},
        )


def _orchestrator(factory=_FakeScientist):
    orchestrator = ProposerOrchestrator(
        model=object(), runtime=object(), timeout_seconds=30,
        command_timeout_seconds=5, command_output_cap_chars=1000,
    )
    agents = []
    def new_proposer():
        agent = factory()
        agents.append(agent)
        return agent
    orchestrator._new_proposer = new_proposer
    return orchestrator, agents


def _run(orchestrator, tmp_path, **overrides):
    kwargs = {
        "goal": "lower cost", "editable": ["src/**"],
        "frozen": ["tests/**"], "memory_service": object(),
        "base_sha": "abc", "source_path": tmp_path, "repo_path": tmp_path,
        "run_dir": tmp_path, "current_round": 2,
        "candidates_per_round": 5, "gate_block": "must pass",
        "prompt_dir": None, "scientist_steps": 42, "random_seed": 19,
    }
    kwargs.update(overrides)
    return orchestrator.run(**kwargs)


def test_orchestrator_constructs_one_independent_scientist_per_lane(tmp_path):
    orchestrator, agents = _orchestrator()
    result = _run(orchestrator, tmp_path)

    assert not hasattr(orchestrator, "generator")
    assert len(agents) == 3 and len({id(agent) for agent in agents}) == 3
    assert sorted(agent.calls[0]["select_quota"] for agent in agents) == [1, 2, 2]
    assert all(agent.calls[0]["scientist_steps"] == 42 for agent in agents)
    assert len(result.proposals) == 5
    assert result.deliberation_telemetry["mode"] == "scientist"
    assert result.deliberation_telemetry["scientist_steps"] == 42


def test_same_seed_assigns_same_ops(tmp_path):
    first, first_agents = _orchestrator()
    second, second_agents = _orchestrator()
    _run(first, tmp_path, candidates_per_round=4, random_seed=23)
    _run(second, tmp_path, candidates_per_round=4, random_seed=23)
    assert sorted(agent.calls[0]["assigned_ops"] for agent in first_agents) == sorted(
        agent.calls[0]["assigned_ops"] for agent in second_agents
    )


def test_orchestrator_reports_honest_incomplete_when_no_lane_submits(tmp_path):
    class Incomplete:
        def run_lane(self, **kwargs):
            return ScientistResult(
                outcome="research_incomplete", reason="budget exhausted",
                trace={"current_phase": "explore", "outcome": "research_incomplete"},
            )

    orchestrator, _ = _orchestrator(Incomplete)
    result = _run(orchestrator, tmp_path, candidates_per_round=2)
    assert result.abstained is True
    assert result.proposals == []
    assert result.deliberation_telemetry["n_research_incomplete"] == 1
    assert result.trace["lanes"][0]["reason"] == "budget exhausted"


def test_lane_trace_flattens_scientist_observability(tmp_path):
    orchestrator, _ = _orchestrator()
    result = _run(orchestrator, tmp_path, candidates_per_round=2)
    lane = result.trace["lanes"][0]
    assert lane["assigned_generative_ops"]
    assert lane["current_phase"] == "deepen"
    assert lane["outcome"] == "proposals"
