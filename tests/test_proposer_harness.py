from __future__ import annotations

import json

import pytest

from scripts import proposer_harness as harness
from simpleloop.memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)
def _proposal(
    *, instruction, research_target, evidence_refs=(),
    affected_scope="src/default.cc",
):
    return ResearchProposal(
        instruction=instruction,
        research_target=research_target,
        model_claim_refs=("M1",),
        explanation_refs=("E1",),
        hypothesis_id="H1",
        evidence_refs=evidence_refs,
        mechanism="remove repeated work",
        prediction="work count falls while gates remain satisfied",
        affected_scope=affected_scope,
    )




def _history_row(round_id: int, *, parent: str, selected: str | None) -> dict:
    return {
        "round": round_id,
        "parent_sha": parent,
        "selected_candidate": 0 if selected else None,
        "selected_sha": selected,
        "candidates": [],
    }


def test_proposal_to_dict_preserves_new_target():
    proposal = _proposal(
        instruction="change the data layout",
        research_target=NewFindingTarget(
            question="Can SoA remove repeated gathers?",
            mechanisms=("SoA",),
            code_regions=("src/fcn.cc",),
        ),
        evidence_refs=("source:src/fcn.cc:42",),
        affected_scope="moves ownership across the interface",
    )

    assert harness._proposal_to_dict(proposal) == {
        "instruction": "change the data layout",
        "research_target": {
            "mode": "new",
            "question": "Can SoA remove repeated gathers?",
            "mechanisms": ["SoA"],
            "code_regions": ["src/fcn.cc"],
        },
        "model_claim_refs": ["M1"],
        "explanation_refs": ["E1"],
        "hypothesis_id": "H1",
        "mechanism": "remove repeated work",
        "prediction": "work count falls while gates remain satisfied",
        "affected_scope": "moves ownership across the interface",
        "evidence_refs": ["source:src/fcn.cc:42"],
        "affected_scope": "moves ownership across the interface",
    }


def test_proposal_to_dict_preserves_existing_target():
    proposal = _proposal(
        instruction="continue the indexed path",
        research_target=ExistingFindingTarget(finding_id="F-012"),
    )

    assert harness._proposal_to_dict(proposal)["research_target"] == {
        "mode": "existing",
        "finding_id": "F-012",
    }


def test_history_state_keeps_last_selected_sha(tmp_path):
    rows = [
        _history_row(0, parent="base", selected="winner"),
        _history_row(1, parent="winner", selected=None),
    ]
    history_dir = tmp_path / "source-run"
    history_dir.mkdir()
    (history_dir / "history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    assert harness._history_state(history_dir, "base") == ("winner", 2)


def test_history_state_uses_baseline_for_empty_history(tmp_path):
    history_dir = tmp_path / "source-run"
    history_dir.mkdir()

    assert harness._history_state(history_dir, "base") == ("base", 0)


def test_history_state_rejects_malformed_history(tmp_path):
    history_dir = tmp_path / "source-run"
    history_dir.mkdir()
    (history_dir / "history.jsonl").write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="could not read history"):
        harness._history_state(history_dir, "base")


def test_render_markdown_numbers_final_proposals():
    result = {
        "status": "completed",
        "input": {
            "base_sha": "abc123",
            "history_source": None,
            "seed": 42,
        },
        "proposals": [
            harness._proposal_to_dict(_proposal(
                instruction="first change",
                research_target=NewFindingTarget(question="first question"),
                evidence_refs=("source:src/a.cc:foo",),
            )),
            harness._proposal_to_dict(_proposal(
                instruction="second change",
                research_target=ExistingFindingTarget(finding_id="F-003"),
                affected_scope="different ownership boundary",
            )),
        ],
    }

    report = harness._render_proposals_markdown(result)

    assert "# Proposer Test Result" in report
    assert "Base SHA: `abc123`" in report
    assert "## Proposal 1" in report
    assert "first change" in report
    assert "source:src/a.cc:foo" in report
    assert "## Proposal 2" in report
    assert "F-003" in report
    assert "different ownership boundary" in report
