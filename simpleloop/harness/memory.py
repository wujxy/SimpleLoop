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
            return [json.loads(line) for line in stream if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read history memory {path}: {exc}") from exc


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
        "family": candidate.get("family") or "single",
        "proposal": candidate.get("proposal") or "",
        "parent_sha": record.get("parent_sha"),
        "candidate_sha": candidate.get("sha"),
        "selected": bool(candidate.get("selected")),
        "accepted": bool(candidate.get("accepted")),
        "metrics": candidate.get("metrics") or {},
        "risk": candidate.get("risk"),
        "feedback_for_proposer": candidate.get("feedback_for_proposer") or "",
        "changed_paths": candidate.get("changed_paths") or [],
    }


def load_insights(path: Path) -> list[dict]:
    """Read and validate the compact append-only Insight JSONL."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not read insight memory {path}: {exc}"
        ) from exc
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"id", "text", "refs"}
            or not isinstance(record["id"], str)
            or not isinstance(record["text"], str)
            or not isinstance(record["refs"], list)
            or not all(isinstance(ref, str) for ref in record["refs"])
        ):
            raise ValueError(
                f"invalid insight memory record in {path}: {record!r}"
            )
    return records


def render_insights(insights: list[dict]) -> str:
    """Render compact semantic memory for the Proposer prompt."""
    if not insights:
        return "  (none yet)"
    return "\n\n".join(
        "[{}] {}\nEvidence: {}".format(
            item["id"], item["text"], ", ".join(item["refs"])
        )
        for item in insights
    )


def validate_insight(
    text: str,
    refs: list[str],
    history: list[dict],
) -> tuple[str, list[str]] | None:
    """Normalize one optional Insight and prove every evidence ref exists."""
    normalized_text = str(text).strip()
    normalized_refs = [str(ref).strip() for ref in refs]
    if not normalized_text:
        if normalized_refs:
            raise ValueError("empty insight must have empty insight_refs")
        return None
    if not normalized_refs:
        raise ValueError("non-empty insight requires at least one insight_ref")
    for ref in normalized_refs:
        resolve_episode(history, ref)
    return normalized_text, normalized_refs


def append_insight(
    path: Path,
    round_id: int,
    text: str,
    refs: list[str],
) -> bool:
    """Append I<round> once; an identical replay is a no-op."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": f"I{round_id}", "text": text, "refs": refs}
    existing = load_insights(path)
    same_id = next(
        (item for item in existing if item["id"] == record["id"]),
        None,
    )
    if same_id == record:
        return False
    if same_id is not None:
        raise ValueError("conflicting insight " + record["id"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True
