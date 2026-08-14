"""Host-owned proposer input/output contracts."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from simpleloop.stages.proposer import (
    Abstention,
    Proposal,
    ProposalBatch,
    ProposerRequest,
    decode_lane_proposals,
)


def test_host_proposer_contract_is_small_and_frozen():
    proposal = Proposal("try cache", ("src/a.cc:10",))
    request = ProposerRequest(3, "make it faster", "abc")
    batch = ProposalBatch((proposal,))

    assert request.incumbent_sha == "abc"
    assert batch.abstention is None
    assert batch.abstained is False
    assert not hasattr(proposal, "research_target")
    assert not hasattr(proposal, "material_difference")
    with pytest.raises(FrozenInstanceError):
        proposal.instruction = "mutate"


def test_empty_batch_has_explicit_abstention():
    batch = ProposalBatch(
        (), Abstention("no useful experiment", "missing profile")
    )

    assert batch.abstained is True
    assert batch.abstention.blocking_unknown == "missing profile"


def test_lane_decoder_keeps_only_host_proposal_facts():
    proposals = decode_lane_proposals([{
        "instruction": "try cache",
        "research_target": {"question": "why?"},
        "evidence_refs": ["src/a.cc:10"],
        "material_difference": "new scope",
    }])

    assert proposals == (Proposal("try cache", ("src/a.cc:10",)),)


def test_proposal_rejects_empty_instruction():
    with pytest.raises(ValueError, match="instruction"):
        Proposal("  ")
