"""ProposerLaneWorker: one proposer lane's research episode in its own
writable workspace -> proposals.

The lane workspace lifecycle belongs to the execution backends (they create
one workspace per lane before, remove it after); job management belongs to
HEPJobBackend. The worker is launchable standalone:

  python -m simpleloop.proposer_lane_worker --manifest /path/manifest.json \
      --job-id 12345.0

Completion contract (identical to CandidateWorker): result.json + usage.json
are written atomically and _FINISHED is touched last in result_dir. ANY
business-side failure still produces all of them — a missing _FINISHED means
the process was killed by infrastructure (condor_rm/OOM/node death), and only
then is a job-level retry meaningful.

Import-light: does not import simpleloop.loop (matplotlib). The orchestrator /
generator / proposer / memory chain has no matplotlib dependency.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from . import config as config_mod
from .candidate_worker import write_result


def _argv_value(flag: str) -> str | None:
    """Return the value following ``flag`` in sys.argv, or None if absent. Used by
    ``_redirect_self_repo`` (which runs before argparse) to find --self-repo /
    --manifest without parsing the whole CLI."""
    try:
        i = sys.argv.index(flag)
    except ValueError:
        return None
    if i + 1 >= len(sys.argv):
        return None
    return sys.argv[i + 1]


def _redirect_self_repo() -> None:
    """Prepend the active self-repo to sys.path so ``import proposer`` (the imports
    below) resolves to the run-local self instead of the installed package.

    Two resolution paths, in precedence order:
      - ``--self-repo <path>`` (S3b): the explicit candidate self-repo, used by the
        viability ``--check`` mode. Takes precedence.
      - ``--manifest <path>`` (S3a): a lane manifest whose ``run_dir`` locates the
        incumbent active self at ``<run_dir>/self/repo``. Used in normal task mode.

    MUST run before the first ``from proposer...`` import below, so it is invoked at
    module import time (ahead of ``main()``). Fully defensive: any failure or an absent
    self-repo falls through to the installed ``proposer/`` — the historical behavior —
    so old run-dirs and manifest-less invocations are unaffected.
    """
    path = _argv_value("--self-repo")
    if path is None:
        manifest_path = _argv_value("--manifest")
        if manifest_path is None:
            return
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        run_dir = manifest.get("run_dir") if isinstance(manifest, dict) else None
        if not run_dir:
            return
        path = str(Path(run_dir).resolve() / "self" / "repo")
    if path:
        resolved = Path(path).resolve()
        if resolved.is_dir():
            sys.path.insert(0, str(resolved))


_redirect_self_repo()

import proposer  # the loaded self (redirected above); reads CONTRACT_VERSION
from proposer.runtime import ApptainerRuntime, world_mount_map
from .harness import views
from .harness.workspace import Workspace
from proposer.memory import MemoryService
from proposer.memory.models import (
    ExistingFindingTarget, NewFindingTarget, ResearchProposal,
)
from proposer import model as model_mod
from proposer.orchestrator import ProposerOrchestrator
from proposer.scientist import ContextPolicy


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ProposerLaneSpec:
    """Serializable input for one proposer lane; persisted as manifest.json."""
    lane_id: int
    round_id: int
    base_sha: str
    run_dir: str = ""
    workspace_path: str = ""
    result_dir: str = ""
    prompt_dir: str = ""
    proposal_slots: int = 1
    scientist_steps: int = 200
    attempt: int = 1
    mode: str = "task"  # "task" (default) | "self" (RSI S3c self-review)

    def to_dict(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "round_id": self.round_id,
            "base_sha": self.base_sha,
            "run_dir": self.run_dir,
            "workspace_path": self.workspace_path,
            "result_dir": self.result_dir,
            "prompt_dir": self.prompt_dir,
            "proposal_slots": self.proposal_slots,
            "scientist_steps": self.scientist_steps,
            "attempt": self.attempt,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProposerLaneSpec":
        # Tolerate unknown fields so a --continue across the S2c version
        # boundary does not crash on manifests written by older code.
        return cls(
            lane_id=int(data["lane_id"]),
            round_id=int(data["round_id"]),
            base_sha=str(data["base_sha"]),
            run_dir=str(data.get("run_dir") or ""),
            workspace_path=str(data.get("workspace_path") or ""),
            result_dir=str(data.get("result_dir") or ""),
            prompt_dir=str(data.get("prompt_dir") or ""),
            proposal_slots=int(data.get("proposal_slots") or 1),
            scientist_steps=int(data.get("scientist_steps") or 200),
            attempt=int(data.get("attempt") or 1),
            mode=str(data.get("mode") or "task"),
        )


@dataclass
class ProposerLaneDeps:
    """Runtime fixtures run_lane needs. Rebuilt from the resolved config on a
    compute node (the frontend owns the shared repo; the worker only reads the
    pre-created lane workspace)."""
    cfg: dict
    run_dir: Path
    runtime: ApptainerRuntime
    workspace: Workspace
    orchestrator: ProposerOrchestrator
    memory_service: MemoryService
    prompt_dir: Path | None = None
    gate_lines: str = ""


def build_lane_deps(
    cfg: dict,
    run_dir: str | Path,
    *,
    usage_observer=None,
    prompt_dir: str | Path | None = None,
) -> ProposerLaneDeps:
    """Rebuild proposer-lane worker dependencies from a resolved config. Does
    NOT call workspace.setup() (no clone) — the frontend owns the shared repo;
    the worker only reads the lane workspace the backend created for it."""
    run_dir = Path(run_dir)
    runtime = ApptainerRuntime(
        image=cfg["runtime_image"],
        binds=cfg["runtime_binds"],
        run_dir=run_dir,
    )
    researcher = (cfg.get("roles") or {}).get("researcher") or {}
    timeout = cfg.get("agent_timeout_seconds", 3600)
    orchestrator = ProposerOrchestrator(
        model=model_mod.build_chat_model(researcher),
        runtime=runtime,
        timeout_seconds=timeout,
        command_timeout_seconds=researcher.get("command_timeout_seconds", 120),
        command_output_cap_chars=researcher.get("command_output_cap_chars", 12000),
        usage_observer=usage_observer,
        context_policy=ContextPolicy.from_config(cfg.get("context")),
    )
    workspace = Workspace(
        run_dir=run_dir,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    memory_service = MemoryService(
        run_dir=run_dir, metrics_schema=cfg.get("metrics") or {},
    )
    return ProposerLaneDeps(
        cfg=cfg, run_dir=run_dir, runtime=runtime, workspace=workspace,
        orchestrator=orchestrator, memory_service=memory_service,
        prompt_dir=Path(prompt_dir) if prompt_dir else None,
        gate_lines=views.gate_block(cfg.get("metrics")),
    )


def _proposal_to_dict(proposal) -> dict:
    """ResearchProposal -> plain dict (research_target recurses)."""
    return dataclasses.asdict(proposal)


def _target_from_dict(d: dict):
    """Inverse of asdict on ExistingFindingTarget/NewFindingTarget. asdict drops
    the type tag, so infer from the keys: finding_id -> existing, else new."""
    if not isinstance(d, dict):
        return NewFindingTarget(question=str(d))
    if "finding_id" in d:
        return ExistingFindingTarget(finding_id=str(d["finding_id"]))
    return NewFindingTarget(
        question=str(d.get("question") or ""),
        mechanisms=tuple(d.get("mechanisms") or ()),
        code_regions=tuple(d.get("code_regions") or ()),
    )


def proposal_from_dict(d: dict):
    """plain dict -> ResearchProposal (the inverse the backend uses to rebuild
    proposals from a lane worker's result.json)."""
    return ResearchProposal(
        instruction=str(d.get("instruction") or ""),
        research_target=_target_from_dict(d.get("research_target") or {}),
        evidence_refs=tuple(d.get("evidence_refs") or ()),
        material_difference=d.get("material_difference"),
    )


def _lane_result_to_dict(lr) -> dict:
    proposals = (
        [_proposal_to_dict(p) for p in lr.proposals] if lr.proposals else []
    )
    return {
        "status": "COMPLETED",
        "lane_id": lr.lane_id,
        "outcome": lr.outcome,
        "proposals": proposals,
        "reason_kind": lr.reason_kind,
        "explanation": lr.explanation,
        "abstain_reason": lr.abstain_reason,
        "trace": lr.trace or {},
        "telemetry": lr.deliberation_telemetry or {},
    }


def _failure_result(spec: ProposerLaneSpec, reason: str) -> dict:
    return {
        "status": "LANE_FAILED",
        "lane_id": spec.lane_id,
        "outcome": "error",
        "proposals": [],
        "reason_kind": None,
        "explanation": reason,
        "enrichment_partial": False,
        "abstain_reason": reason,
        "trace": {},
        "telemetry": {"tool_calls": 0},
    }


def run_lane(deps: ProposerLaneDeps, spec: ProposerLaneSpec) -> dict:
    """Run one lane episode in ``spec.workspace_path`` and return the result
    dict. The workspace must already exist (the backend created it)."""
    cfg = deps.cfg
    workspace = Path(spec.workspace_path)
    lane_result = deps.orchestrator.run_lane_episode(
        lane_id=spec.lane_id,
        workspace=workspace,
        base_sha=spec.base_sha,
        goal=cfg["goal"],
        editable=cfg["editable_paths"],
        # frozen is mount-enforced (EROFS outside editable); vestigial list
        # kept for call-site compatibility.
        frozen=[],
        world_mount=world_mount_map(cfg),
        memory_service=deps.memory_service,
        repo_path=deps.workspace.repo,
        run_dir=deps.run_dir,
        current_round=spec.round_id,
        gate_block=deps.gate_lines,
        prompt_dir=deps.prompt_dir,
        hints=cfg.get("hints") or None,
        proposal_slots=spec.proposal_slots,
        scientist_steps=spec.scientist_steps,
    )
    return _lane_result_to_dict(lane_result)


def _run_check(self_repo: str | Path, run_dir: str | Path,
               expected_version: str) -> int:
    """S3b viability self-check. Runs offline (no model, no network) against the
    CANDIDATE proposer — which ``_redirect_self_repo`` already loaded from
    ``--self-repo`` at module import time, so reaching here means boot (1) passed.

    Asserts:
      (2) contract version — ``proposer.CONTRACT_VERSION`` == the Host's expected.
      (3) continuity load — the candidate can read run_dir/proposer/ continuity
          without crashing (exercised on a TEMP COPY so the check never mutates the
          run it is evaluating).
      (4) protocol produce — the candidate can construct a legal output skeleton
          (one synthetic proposal + an abstain) and serialize it, without the model.

    Exit 0 = viable; 1 = broken self (each failure printed to stderr as
    ``[check] FAIL: …``). Per contract §8 / RSI §19: capability ("is it stronger")
    is NOT in scope — that is later real task progress.
    """
    import proposer  # the candidate (loaded by the redirect above)
    errors: list[str] = []

    # (2) contract version
    declared = getattr(proposer, "CONTRACT_VERSION", None)
    if declared != expected_version:
        errors.append(
            f"contract version mismatch: proposer declares {declared!r}, "
            f"Host expects {expected_version!r}")

    # (3) continuity load — on a temp copy of run_dir/proposer, side-effect-free
    try:
        from proposer.scientist_session import ScientistSession
        from proposer.scientist import SCIENTIST_PROMPT_VERSION
        with TemporaryDirectory() as td:
            probe_run = Path(td)
            src = Path(run_dir) / "proposer"
            if src.is_dir():
                shutil.copytree(src, probe_run / "proposer")
            ScientistSession.load_or_create(
                probe_run, 0, prompt_version=SCIENTIST_PROMPT_VERSION)
    except Exception as exc:  # noqa: BLE001 — any failure = broken continuity reader
        errors.append(f"continuity load failed: {exc}")

    # (4) protocol produce — legal output skeletons from the candidate's data model
    try:
        from proposer.memory.models import NewFindingTarget, ResearchProposal
        from proposer.orchestrator import LaneResult
        probe = ResearchProposal(
            "viability probe", NewFindingTarget(question="probe"), (), None)
        LaneResult(lane_id=0, proposals=(probe,), outcome="submit")  # submit skeleton
        LaneResult(lane_id=0, proposals=(), outcome="abstain",       # abstain skeleton
                   abstain_reason="viability check")
        json.dumps(dataclasses.asdict(probe))                        # serializable
    except Exception as exc:  # noqa: BLE001 — any failure = broken output schema
        errors.append(f"protocol produce failed: {exc}")

    if errors:
        for err in errors:
            print(f"[check] FAIL: {err}", file=sys.stderr, flush=True)
        return 1
    print(f"[check] OK: viable (contract={declared}, self_repo={Path(self_repo)})",
          flush=True)
    return 0


def _self_review_result_to_dict(spec: ProposerLaneSpec, result) -> dict:
    """SelfReviewResult -> the self-mode result.json shape (RSI S3c). Distinct
    from the lane shape; consumed only by the Host self-review path (S3c.2)."""
    sc = result.self_change or {}
    return {
        "status": "COMPLETED",
        "mode": "self",
        "lane_id": spec.lane_id,
        "round_id": spec.round_id,
        "self_review": {
            "contract_version": proposer.CONTRACT_VERSION,
            "incumbent_self_sha": None,  # filled by run_self_review_lane
            "decision": result.decision,
            "diagnosis": result.diagnosis,
            "keep_reason": result.keep_reason,
            "next_review_after_rounds": result.next_review_after_rounds,
            "self_change": (
                {
                    "target": sc.get("target"),
                    "intent": sc.get("intent"),
                    "instruction": sc.get("instruction"),
                    "evidence_refs": list(sc.get("evidence_refs") or ()),
                } if sc else None
            ),
            "abstained": result.abstained,
        },
        "trace": result.trace or {},
        "telemetry": result.deliberation_telemetry or {},
    }


def run_self_review_lane(deps: ProposerLaneDeps, spec: ProposerLaneSpec) -> dict:
    """Run one self-review episode and return the self-mode result dict. The
    workspace is the incumbent self-repo (the Scientist reads its own source);
    the incumbent SHA + reviews path come from run_dir/self/."""
    from .self_repo import SelfRepo
    sr = SelfRepo(deps.run_dir)
    metrics = deps.cfg.get("metrics") or {}
    objective_key = (metrics.get("objective") or {}).get("key")
    result = deps.orchestrator.run_self_review(
        self_repo=sr.repo,
        run_dir=deps.run_dir,
        reviews_path=sr.root / "reviews.jsonl",
        incumbent_self_sha=sr.active_self_sha,
        goal=deps.cfg["goal"],
        objective_key=objective_key,
        current_round=spec.round_id,
        prompt_dir=deps.prompt_dir,
        memory_service=deps.memory_service,
        scientist_steps=spec.scientist_steps,
    )
    out = _self_review_result_to_dict(spec, result)
    out["self_review"]["incumbent_self_sha"] = sr.active_self_sha
    return out


def _self_review_failure_result(spec: ProposerLaneSpec, reason: str) -> dict:
    """A self-mode terminal result for an infrastructure/business failure: a
    default KEEP with a short defer so the Host's scheduler re-opens
    self-attention soon (mirrors the orchestrator's crash default)."""
    return {
        "status": "COMPLETED",
        "mode": "self",
        "lane_id": spec.lane_id,
        "round_id": spec.round_id,
        "self_review": {
            "contract_version": proposer.CONTRACT_VERSION,
            "incumbent_self_sha": None,
            "decision": "KEEP",
            "diagnosis": reason,
            "keep_reason": "self-review did not complete; defaulting to KEEP "
                           "pending a successful review",
            "next_review_after_rounds": 3,
            "self_change": None,
            "abstained": True,
        },
        "trace": {},
        "telemetry": {"tool_calls": 0},
    }


def main(argv: list[str] | None = None) -> int:
    """Standalone worker entry. Two modes:

    - ``--check`` (S3b): viability self-check on a candidate self-repo. Exit 0 =
      viable, 1 = broken self. Does not run a lane and writes no result.json.
    - lane mode (default): run one proposer lane from ``--manifest``. Exit 0 whenever
      a terminal result could be written (business failures included); non-zero only
      when the manifest/result path is unusable — infrastructure-grade failure.
    """
    parser = argparse.ArgumentParser(prog="proposer_lane_worker")
    parser.add_argument("--manifest", default=None,
                        help="Path to the proposer-lane manifest JSON (lane mode).")
    parser.add_argument("--job-id", default=None,
                        help="Scheduler job id (recorded in result.execution).")
    parser.add_argument("--check", action="store_true",
                        help="Viability self-check mode (S3b). Requires --self-repo, "
                             "--run-dir, --contract-version. Writes no result.")
    parser.add_argument("--self-repo", default=None,
                        help="Candidate self-repo root (contains proposer/) to check.")
    parser.add_argument("--run-dir", default=None,
                        help="Run dir (for continuity load during --check).")
    parser.add_argument("--contract-version", default=None,
                        help="Contract version the Host expects (checked against "
                             "proposer.CONTRACT_VERSION during --check).")
    args = parser.parse_args(argv)

    if args.check:
        missing = [n for n, v in (("--self-repo", args.self_repo),
                                  ("--run-dir", args.run_dir),
                                  ("--contract-version", args.contract_version))
                   if not v]
        if missing:
            parser.error("--check requires " + ", ".join(missing))
        return _run_check(args.self_repo, args.run_dir, args.contract_version)

    if not args.manifest:
        parser.error("--manifest is required in lane mode "
                     "(or use --check for the viability self-check)")

    usage: list = []
    spec_dict: dict = {}
    result: dict
    try:
        spec_dict = json.loads(
            Path(args.manifest).read_text(encoding="utf-8"))
        if not isinstance(spec_dict, dict):
            raise ValueError("manifest root must be an object")
        run_dir = Path(spec_dict["run_dir"])
        cfg = config_mod.load_resolved(run_dir)
        spec = ProposerLaneSpec.from_dict(spec_dict)
        deps = build_lane_deps(
            cfg, run_dir, usage_observer=usage.append,
            prompt_dir=spec.prompt_dir or None,
        )
        deps.runtime.preflight()
        if spec.mode == "self":
            result = run_self_review_lane(deps, spec)
        else:
            result = run_lane(deps, spec)
    except Exception as exc:
        # Catch-all invariant: a business-side failure still produces a
        # terminal result; _FINISHED absence must mean "killed by infra".
        print(f"[{stamp()}] proposer lane worker failed: {exc}", flush=True)
        mode = str(spec_dict.get("mode") or "task")
        spec = ProposerLaneSpec(
            lane_id=int(spec_dict.get("lane_id") or 0),
            round_id=int(spec_dict.get("round_id") or 0),
            base_sha=str(spec_dict.get("base_sha") or ""),
            mode=mode,
        )
        if mode == "self":
            result = _self_review_failure_result(spec, f"worker failed: {exc}")
        else:
            result = _failure_result(spec, f"worker failed: {exc}")
    sidecar = {
        "usage": usage,
        "execution": {
            "backend": "hepjob",
            "job_id": args.job_id,
            "attempt": int(spec_dict.get("attempt") or 1),
            "host": socket.gethostname(),
        },
    }
    result_dir = spec_dict.get("result_dir")
    if not result_dir:
        print(f"[{stamp()}] FATAL: manifest has no result_dir; "
              "nowhere to write the terminal result", flush=True)
        return 2
    try:
        write_result(result_dir, result, sidecar=sidecar)
    except OSError as exc:
        print(f"[{stamp()}] FATAL: could not write result to {result_dir}: "
              f"{exc}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
