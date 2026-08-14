"""Non-authoritative proposal artifacts written before candidate execution."""
from __future__ import annotations

import json
from pathlib import Path

from .handoff import write_handoff
from ..round import RoundRequest
from ..stages.proposer import ProposalBatch


class RoundArtifacts:
    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)

    def record_proposals(
        self, request: RoundRequest, proposals: ProposalBatch,
    ) -> None:
        if proposals.trace:
            trace_dir = self.run_dir / "proposer_traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / f"r{request.round_id}.json").write_text(
                json.dumps(proposals.trace, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        write_handoff(
            self.run_dir,
            request.round_id,
            "proposals.json",
            {
                "round_id": request.round_id,
                "parent_sha": request.incumbent_sha,
                "abstained": proposals.abstained,
                "abstain_reason": (
                    proposals.abstention.reason if proposals.abstention else None
                ),
                "proposals": [
                    {
                        "index": index,
                        "instruction": proposal.instruction,
                        "evidence_refs": list(proposal.evidence_refs),
                    }
                    for index, proposal in enumerate(proposals.proposals)
                ],
                "trace": proposals.trace,
            },
        )
