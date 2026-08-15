# Vendored from simpleloop/harness/memory.py (S2b(ii)) — the frozen history.jsonl reader.
# The Host keeps its own copy (Store reads the same file); keep both in sync per the
# history.jsonl schema, which is the immovable Kernel format.
"""Per-run Experiment Ledger primitives.

``history.jsonl`` is the immutable episodic record of every candidate the
harness has evaluated. This module gives those records stable
``r<round>c<candidate>`` refs and resolves one bounded factual episode.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


_EPISODE_REF_RE = re.compile(r"^r(0|[1-9]\d*)c(0|[1-9]\d*)$")


def read_history(path: Path) -> list[dict]:
    """Read append-only history JSONL, returning an empty list if absent."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read history memory {path}: {exc}") from exc
    required_candidate = {"status", "gate_passed", "eligible"}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(
                f"history memory {path} contains a non-object row"
            )
        cands = row.get("candidates")
        if not isinstance(cands, list):
            raise ValueError(
                f"history memory {path} row is missing 'candidates' list"
            )
        for cand in cands:
            if (not isinstance(cand, dict)
                    or not required_candidate <= cand.keys()):
                raise ValueError(
                    f"history memory {path} does not use the current "
                    "candidate schema"
                )
    return rows


def _parse_episode_ref(ref: str) -> tuple[int, int]:
    match = _EPISODE_REF_RE.fullmatch(str(ref).strip())
    if match is None:
        raise ValueError(
            f"invalid memory reference {ref!r}; "
            "expected r<round>c<candidate>"
        )
    return int(match.group(1)), int(match.group(2))


def resolve_episode(history: list[dict], ref: str) -> dict:
    """Resolve one candidate reference, including its bounded eval output."""
    round_id, candidate_id = _parse_episode_ref(ref)
    record = next(
        (item for item in history if item.get("round") == round_id),
        None,
    )
    if record is None:
        raise ValueError(f"memory reference not found: {ref}")

    candidate = next(
        (
            item for item in (record.get("candidates") or [])
            if item.get("candidate") == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise ValueError(f"memory reference not found: {ref}")

    experiment_id = str(
        candidate.get("experiment_id") or f"r{round_id}c{candidate_id}"
    )
    return {
        "ref": experiment_id,
        "experiment_id": experiment_id,
        "finding_id": candidate.get("finding_id"),
        "proposal": candidate.get("proposal") or "",
        "parent_sha": candidate.get("parent_sha") or record.get("parent_sha"),
        "candidate_sha": candidate.get("sha"),
        "status": candidate.get("status"),
        "selected": bool(candidate.get("selected")),
        "gate_passed": candidate.get("gate_passed"),
        "eligible": candidate.get("eligible"),
        "gates": candidate.get("gates") or {},
        "metrics": candidate.get("metrics") or {},
        "changed_paths": candidate.get("changed_paths") or [],
        "eval_block": candidate.get("eval_block") or "",
        # The experimenter's objective account (absent for rounds run before
        # reports were retained).
        "self_report": candidate.get("self_report"),
    }
