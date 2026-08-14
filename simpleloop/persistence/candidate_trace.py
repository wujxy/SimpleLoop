"""Non-authoritative handoff traces projected from typed candidate facts."""
from __future__ import annotations

from pathlib import Path

from ..candidate import (
    CandidateArtifact,
    CandidateRequest,
    CandidateStatus,
    EvaluationResult,
    ExecutionResult,
    GateDecision,
)
from ..harness.handoff import write_handoff


class HandoffCandidateTrace:
    def __init__(self, run_dir: Path | None):
        self.run_dir = run_dir

    @staticmethod
    def _name(request: CandidateRequest, stage: str) -> str:
        return f"{request.round_id}-c{request.candidate_id}.{stage}.json"

    def record_execution(
        self,
        request: CandidateRequest,
        execution: ExecutionResult,
        artifact: CandidateArtifact | None,
    ) -> None:
        status = (
            "COMMITTED"
            if artifact is not None
            else "EXECUTOR_FAILED"
            if execution.status == "EXECUTOR_FAILED"
            else "NO_CHANGE"
        )
        row = {
            "candidate_id": request.candidate_id,
            "proposal": request.proposal.instruction,
            "parent_sha": request.parent_sha,
            "sha": artifact.sha if artifact else None,
            "status": status,
            "changed_paths": [
                path.as_posix()
                for path in (artifact.changed_paths if artifact else ())
            ],
            "reason": execution.reason,
            "executor_response": execution.output,
            "self_report": (
                dict(execution.self_report)
                if execution.self_report is not None else None
            ),
        }
        if status == "EXECUTOR_FAILED":
            row["error"] = execution.reason
        write_handoff(
            self.run_dir,
            request.round_id,
            self._name(request, "executor"),
            row,
        )

    def record_evaluation(
        self,
        request: CandidateRequest,
        evaluation: EvaluationResult | None,
        gate: GateDecision,
        status: CandidateStatus,
    ) -> None:
        row = {
            "candidate_id": request.candidate_id,
            "status": status.value,
            "metrics": dict(evaluation.metrics) if evaluation else {},
            "gates": {
                name: {"passed": item.passed, "detail": item.detail}
                for name, item in gate.results.items()
            },
            "gate_passed": gate.passed,
            "eligible": gate.eligible,
            "eval_block": evaluation.text if evaluation else "",
            "returncodes": (
                list(evaluation.returncodes) if evaluation else []
            ),
            "commands_ok": bool(
                evaluation
                and evaluation.error is None
                and all(code == 0 for code in evaluation.returncodes)
            ),
        }
        if evaluation and evaluation.error:
            row["error"] = evaluation.error
        write_handoff(
            self.run_dir,
            request.round_id,
            self._name(request, "eval"),
            row,
        )
