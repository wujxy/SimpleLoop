from pathlib import Path

from simpleloop.memory.models import NewFindingTarget, ResearchProposal
from simpleloop.roles.orchestrator import (
    ProposerOrchestrator,
    _lane_quotas,
    _sample_generative_ops,
)
from simpleloop.roles.proposer import ScientistResult


def _proposal(name):
    return ResearchProposal(
        instruction=name,
        research_target=NewFindingTarget(question=f"question {name}"),
    )


def test_lane_quotas_cover_requested_candidates_without_padding():
    assert _lane_quotas(4, 2) == [2, 2]
    assert _lane_quotas(5, 2) == [2, 2, 1]
    assert _lane_quotas(0, 2) == []


def test_each_lane_receives_five_distinct_generative_operators():
    ops = _sample_generative_ops()
    assert len(ops) == 5
    assert len(set(ops)) == 5
    assert all(op.startswith("G") for op in ops)


class _FakeScientist:
    def __init__(self):
        self.calls = []

    def run_lane(self, **kwargs):
        self.calls.append(kwargs)
        quota = kwargs["select_quota"]
        lane_id = len(self.calls) - 1
        return ScientistResult(
            proposals=tuple(
                _proposal(f"lane-{lane_id}-{index}")
                for index in range(quota)
            ),
            outcome="submit",
            deliberation_telemetry={"steps": 8},
            trace={"phase": "deepen"},
        )


def _orchestrator():
    orchestrator = ProposerOrchestrator(
        model=object(), runtime=object(), timeout_seconds=30,
        command_timeout_seconds=5, command_output_cap_chars=1000,
    )
    orchestrator.proposer = _FakeScientist()
    return orchestrator


def test_orchestrator_runs_only_scientist_lanes_and_collects_quota(tmp_path):
    orchestrator = _orchestrator()
    result = orchestrator.run(
        goal="lower cost", editable=["src/**"], frozen=["tests/**"],
        memory_service=object(), base_sha="abc", source_path=tmp_path,
        repo_path=tmp_path, run_dir=tmp_path, current_round=2,
        candidates_per_round=5, gate_block="must pass", prompt_dir=None,
        scientist_steps=42,
    )

    assert not hasattr(orchestrator, "generator")
    assert len(orchestrator.proposer.calls) == 3
    assert sorted(
        call["select_quota"] for call in orchestrator.proposer.calls
    ) == [1, 2, 2]
    assert all(call["scientist_steps"] == 42
               for call in orchestrator.proposer.calls)
    assert len(result.proposals) == 5
    assert result.deliberation_telemetry["mode"] == "scientist"
    assert result.deliberation_telemetry["scientist_steps"] == 42
    assert all("scientist" in lane for lane in result.trace["lanes"])


def test_orchestrator_reports_honest_incomplete_when_no_lane_submits(tmp_path):
    orchestrator = _orchestrator()

    class Incomplete:
        def run_lane(self, **kwargs):
            return ScientistResult(
                outcome="research_incomplete", reason="budget exhausted",
                trace={"phase": "explore"},
            )

    orchestrator.proposer = Incomplete()
    result = orchestrator.run(
        goal="lower cost", editable=["src/**"], frozen=[],
        memory_service=object(), base_sha="abc", source_path=tmp_path,
        repo_path=tmp_path, run_dir=tmp_path, current_round=2,
        candidates_per_round=2, gate_block="must pass", prompt_dir=None,
        scientist_steps=10,
    )

    assert result.abstained is True
    assert result.proposals == []
    assert result.deliberation_telemetry["n_research_incomplete"] == 1
    assert result.trace["lanes"][0]["reason"] == "budget exhausted"
