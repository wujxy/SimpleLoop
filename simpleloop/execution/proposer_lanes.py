"""Backend-agnostic proposer-lane helpers: the file-I/O tail shared by every
execution backend — manifest write, atomic result read, collect -> ProposerResult,
and the inflight marker.

Extracted from HEPJobBackend's private methods so the LOCAL backend can spawn
the proposer (``simpleloop.proposer_lane_worker``) as a subprocess without
duplicating them. HEPJob still carries its own copies for now; migrating it
onto this module is a follow-up. Nothing here knows about condor or Popen —
the backend owns *how the worker runs*; this owns *how its result is written,
read, and turned into a ProposerResult*.

The lane workspace lifecycle is intentionally NOT owned here: the caller
(``run_proposer_lanes``) creates the workspace before spawn and removes it in a
``finally``, so cleanup is uniform across the success and infra-failure paths.
``collect_lane_results`` only reads results and ingests telemetry.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..proposer_lane_worker import proposal_from_dict
from proposer import scientist as proposer_mod
from .base import InfraRoundError

_WORKER_META_NAME = "usage.json"


@dataclass
class LaneJob:
    """One proposer lane's backend-side lifecycle state — the lane analogue of
    a candidate job. Backend-agnostic: condor carries a job_id, local carries a
    pid/Popen; those stay on the backend's own job object, not here. All
    ``collect_lane_results`` needs is ``lane_id`` + ``result_dir`` + ``state``
    + ``result``.

    ``state`` is the *backend* state (did the process reach a terminal
    business result?), distinct from the result dict's internal ``status``
    (COMPLETED vs LANE_FAILED). A lane whose worker wrote ``_FINISHED`` — even
    with a LANE_FAILED result — is COMPLETED here; collect turns an empty
    proposal list into an abstention.
    """

    lane_id: int
    result_dir: Path
    state: str = "SUBMITTED"   # SUBMITTED | COMPLETED | INFRA_FAILED | TIMEOUT
    result: dict | None = None


def lane_result_dir(run_dir: Path, round_id: int, lane_id: int) -> Path:
    """Canonical per-lane artifact directory: ``run_dir/rounds/r{N}/lanes/l{L}``."""
    return Path(run_dir) / "rounds" / f"r{round_id}" / "lanes" / f"l{lane_id}"


def write_lane_manifest(result_dir: Path, spec) -> None:
    """Atomically write the worker's ``manifest.json``."""
    result_dir = Path(result_dir)
    path = result_dir / "manifest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(spec.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)


def read_lane_result(result_dir: Path) -> dict:
    """Pure read + shape check of a lane worker's ``result.json``.

    Raises ``ValueError`` on a missing or malformed file so the caller can
    classify it as an infrastructure failure (the worker writes ``_FINISHED``
    on every business outcome, so an unreadable result means it was killed).
    """
    try:
        result = json.loads(
            (Path(result_dir) / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("result.json is not a proposer-lane result object")
    if (not isinstance(result.get("status"), str)
            or not isinstance(result.get("proposals"), list)):
        raise ValueError("result.json is not a proposer-lane result object")
    return result


def read_worker_meta(result_dir: Path) -> dict:
    """Read the ``usage.json`` sidecar; degrade to ``{}`` when absent/unreadable."""
    try:
        return json.loads(
            (Path(result_dir) / _WORKER_META_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _lane_id(job) -> int:
    """Lane id, accepting both ``LaneJob`` (.lane_id) and HEPJob's ``_Job``
    (.candidate_id, which holds the lane id when a ``_Job`` is reused as a
    proposer-lane job). Uses an explicit None check so lane_id == 0 works."""
    lid = getattr(job, "lane_id", None)
    return lid if lid is not None else getattr(job, "candidate_id", None)


def collect_lane_results(lane_jobs, *, round_id: int, telemetry=None):
    """Turn finished lane jobs into a ``ProposerResult``.

    For each COMPLETED lane: ingest its ``usage.json`` into ``telemetry`` (the
    host never sees proposer model usage otherwise — this is the only path by
    which it is recorded), and rebuild ``ResearchProposal`` objects from the
    result dicts. Raises ``InfraRoundError`` when no lane reached COMPLETED.
    Does NOT remove lane workspaces — the caller owns that lifecycle.
    """
    proposals = []
    lane_traces: list[dict] = []
    lane_telemetries: list[dict] = []
    any_completed = False
    for job in lane_jobs:
        if job.state == "COMPLETED" and job.result is not None:
            any_completed = True
            lane_id = _lane_id(job)
            res = job.result
            meta = read_worker_meta(job.result_dir)
            if telemetry is not None:
                for record in (meta.get("usage") or []):
                    telemetry.record_usage(record)
            lane_proposals = [
                proposal_from_dict(pd) for pd in (res.get("proposals") or [])
            ]
            proposals.extend(lane_proposals)
            lane_traces.append({
                "lane_id": lane_id,
                "outcome": res.get("outcome"),
                "n_proposals": len(lane_proposals),
                "reason_kind": res.get("reason_kind"),
            })
            lane_telemetries.append({
                "lane_id": lane_id,
                "telemetry": res.get("telemetry") or {},
            })
    if not any_completed:
        raise InfraRoundError(
            f"round {round_id}: all {len(lane_jobs)} proposer lane job(s) "
            "failed on infrastructure; the round was not recorded.")
    return proposer_mod.ProposerResult(
        proposals=proposals,
        abstained=(len(proposals) == 0),
        abstain_reason=("all lanes abstained/blocked/errored"
                        if not proposals else None),
        deliberation_telemetry={"lanes": lane_telemetries},
        trace={"lanes": lane_traces},
    )


def inflight_proposer_path(run_dir: Path) -> Path:
    return Path(run_dir) / "inflight_proposer.json"


def write_inflight_proposer(run_dir: Path, round_id: int, base_sha: str,
                            lane_entries: list[dict]) -> None:
    """Record live proposer-lane handles (PIDs for local, job ids for hepjob)
    so a crashed frontend can reap them via ``cleanup_proposer_orphans``."""
    path = inflight_proposer_path(run_dir)
    payload = {"round_id": round_id, "base_sha": base_sha, "lanes": lane_entries}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)


def clear_inflight_proposer(run_dir: Path) -> None:
    inflight_proposer_path(run_dir).unlink(missing_ok=True)
