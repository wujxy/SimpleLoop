"""Tests for the Scientist orchestrator adapter.

The orchestrator owns NO research logic — it loads/resumes the Scientist
session, runs one round, persists the session, and maps the outcome to
LaneResult / ProposerResult. These tests monkeypatch ScientistAgent.research so
no model or container is needed; they verify the session lifecycle, the
result-shape mapping (the interface firewall loop.py depends on), and that the
Scientist persists as the same persona across rounds.
"""
from __future__ import annotations

import json
from pathlib import Path

from simpleloop.memory.models import NewFindingTarget, ResearchProposal
from simpleloop.roles.orchestrator import LaneResult, ProposerOrchestrator
from simpleloop.roles.proposer import ScientistRound


def _make_orch() -> ProposerOrchestrator:
    # model/runtime are never used once research is monkeypatched.
    return ProposerOrchestrator(
        model=object(), runtime=object(), timeout_seconds=60,
        command_timeout_seconds=10, command_output_cap_chars=1000,
    )


def _kwargs(tmp_path, **over):
    base = dict(
        goal="g", editable=["src"], frozen=[], world_mount=None,
        memory_service=None, base_sha="b", repo_path=tmp_path,
        run_dir=tmp_path, current_round=0, gate_block="g", prompt_dir=None,
    )
    base.update(over)
    return base


# ---- result-shape mapping (the loop.py interface firewall) ----

def test_run_maps_submit_to_proposer_result(monkeypatch, tmp_path):
    orch = _make_orch()

    def fake_research(**kw):
        return ScientistRound(proposals=[
            ResearchProposal(instruction="dir A",
                             research_target=NewFindingTarget(question="qA")),
            ResearchProposal(instruction="dir B",
                             research_target=NewFindingTarget(question="qB")),
        ])

    monkeypatch.setattr(orch.scientist, "research", fake_research)
    result = orch.run(workspaces=[tmp_path], candidates_per_round=4,
                      scientist_steps=10, **_kwargs(tmp_path))
    assert not result.abstained
    assert len(result.proposals) == 2
    assert result.proposals[0].instruction == "dir A"
    # trace + telemetry present (loop.py forwards them)
    assert "lanes" in result.trace
    assert result.deliberation_telemetry["n_proposals"] == 2


def test_run_maps_abstain_to_proposer_result(monkeypatch, tmp_path):
    orch = _make_orch()
    monkeypatch.setattr(
        orch.scientist, "research",
        lambda **kw: ScientistRound(proposals=[], abstained=True,
                                    abstain_reason="nothing worth it"),
    )
    result = orch.run(workspaces=[tmp_path], candidates_per_round=2,
                      scientist_steps=10, **_kwargs(tmp_path))
    assert result.abstained
    assert result.proposals == []
    assert "nothing worth it" in (result.abstain_reason or "")


def test_run_lane_episode_returns_lane_result(monkeypatch, tmp_path):
    orch = _make_orch()
    monkeypatch.setattr(
        orch.scientist, "research",
        lambda **kw: ScientistRound(proposals=[
            ResearchProposal(instruction="x",
                             research_target=NewFindingTarget(question="q"))]),
    )
    lr = orch.run_lane_episode(lane_id=0, workspace=tmp_path,
                               proposal_slots=1, scientist_steps=10,
                               **_kwargs(tmp_path))
    assert isinstance(lr, LaneResult)
    assert lr.outcome == "submit"
    assert len(lr.proposals) == 1


def test_run_lane_episode_error_maps_to_error_outcome(monkeypatch, tmp_path):
    orch = _make_orch()

    def boom(**kw):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(orch.scientist, "research", boom)
    lr = orch.run_lane_episode(lane_id=0, workspace=tmp_path,
                               proposal_slots=1, scientist_steps=10,
                               **_kwargs(tmp_path))
    assert lr.outcome == "error"
    assert "model exploded" in (lr.abstain_reason or "")


# ---- session lifecycle: the Scientist persists across rounds ----

def test_session_persists_across_rounds_same_persona(monkeypatch, tmp_path):
    """Round 0 cold-starts and leaves a marker; round 1 resumes and the
    orchestrator hands research a session that already carries round 0's
    trajectory — same scientist_id throughout."""
    orch = _make_orch()
    seen: list[tuple[int, str, bool]] = []

    def fake_research(*, session, current_round, **kw):
        seen.append((current_round, session.scientist_id,
                     session.is_first_round()))
        session.append_message(
            "assistant", f"round{current_round}-marker",
            round_id=current_round,
        )
        session.write_notebook(f"note from round {current_round}")
        return ScientistRound(proposals=[
            ResearchProposal(instruction="d",
                             research_target=NewFindingTarget(question="q"))])

    monkeypatch.setattr(orch.scientist, "research", fake_research)

    orch.run_lane_episode(lane_id=0, workspace=tmp_path,
                          proposal_slots=1, scientist_steps=10,
                          **_kwargs(tmp_path, current_round=0, base_sha="a"))
    orch.run_lane_episode(lane_id=0, workspace=tmp_path,
                          proposal_slots=1, scientist_steps=10,
                          **_kwargs(tmp_path, current_round=1, base_sha="b"))

    assert seen[0][2] is True    # round 0 = cold start
    assert seen[1][2] is False   # round 1 = resume
    assert seen[0][1] == seen[1][1]  # same scientist_id (stable identity)

    # meta.json persisted with the last round + the round-1 notebook survived
    meta = json.loads(
        (tmp_path / "scientists" / "lane-0" / "meta.json").read_text())
    assert meta["last_round"] == 1
    assert meta["scientist_id"] == seen[0][1]
    notebook = (tmp_path / "scientists" / "lane-0" / "notebook.md").read_text()
    assert "round 1" in notebook
    # round-0 trajectory is in the immutable archive
    archive = (tmp_path / "scientists" / "lane-0"
               / "session.jsonl").read_text()
    assert "round0-marker" in archive
