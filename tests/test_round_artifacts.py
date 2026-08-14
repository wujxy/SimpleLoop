from __future__ import annotations

import json

from simpleloop.persistence.round_artifacts import RoundArtifacts
from simpleloop.round import RoundRequest, SelectionPolicy
from simpleloop.stages.proposer import Abstention, Proposal, ProposalBatch


def test_round_artifacts_write_trace_and_pre_execution_handoff(tmp_path):
    recorder = RoundArtifacts(tmp_path)
    request = RoundRequest(
        3, "goal", "parent", {"OBJ": 10}, SelectionPolicy("OBJ", True),
    )
    proposals = ProposalBatch(
        (Proposal("try cache", ("src/a.py",)),),
        trace={"steps": ["inspect"]},
    )

    recorder.record_proposals(request, proposals)

    trace = json.loads((tmp_path / "proposer_traces" / "r3.json").read_text())
    handoff = json.loads(
        (tmp_path / "handoffs" / "r3" / "proposals.json").read_text()
    )
    assert trace == {"steps": ["inspect"]}
    assert handoff["parent_sha"] == "parent"
    assert handoff["proposals"] == [{
        "index": 0, "instruction": "try cache", "evidence_refs": ["src/a.py"],
    }]


def test_round_artifacts_record_abstention_without_empty_trace_file(tmp_path):
    recorder = RoundArtifacts(tmp_path)
    request = RoundRequest(
        1, "goal", "base", {}, SelectionPolicy("OBJ", True),
    )

    recorder.record_proposals(
        request, ProposalBatch((), Abstention("nothing worth running")),
    )

    assert not (tmp_path / "proposer_traces" / "r1.json").exists()
    handoff = json.loads(
        (tmp_path / "handoffs" / "r1" / "proposals.json").read_text()
    )
    assert handoff["abstained"] is True
    assert handoff["abstain_reason"] == "nothing worth running"
