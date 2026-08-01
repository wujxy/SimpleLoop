"""Per-run Search Memory primitives.

`history.jsonl` remains the complete episodic record.  This module gives those
records stable `r<round>c<candidate>` references and returns a compact,
Proposer-facing view of one referenced candidate.
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
    required = {"status", "gate_passed", "eligible"}
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("candidates"), list)
        or any(
            not isinstance(candidate, dict)
            or not required <= candidate.keys()
            for candidate in row["candidates"]
        )
        for row in rows
    ):
        raise ValueError(
            f"history memory {path} does not use the current candidate schema"
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
    """Resolve one candidate reference without exposing noisy raw eval output."""
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

    return {
        "ref": f"r{round_id}c{candidate_id}",
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
    }
