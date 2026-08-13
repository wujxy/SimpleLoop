"""Append-only Finding Archive.

Findings live at ``<run_dir>/proposer/findings.jsonl`` (the proposer-owned
Autobiography location; legacy run-dirs keep ``<run_dir>/memory/findings.jsonl``
— see ``_resolve_findings_path``). Every mutation (create, commit a round's
experiment refs, change state) appends a new full record; the current state of
finding ``F-NNN`` is the last record with that id. This mirrors
``history.jsonl``'s audit-friendly semantics — nothing is ever silently
rewritten, and a crashed writer leaves at most one truncated last line
(recoverable by ignoring the tail).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Finding


_FINDING_ID_RE = re.compile(r"^F-(\d{3,})$")


class FindingStore:
    """Owns findings.jsonl reads and appends."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self._resolve_findings_path(self.run_dir)

    @staticmethod
    def _resolve_findings_path(run_dir: Path) -> Path:
        """Resolve the findings path ONCE at construction; reads and appends
        then use the same path so an append-only archive never splits across
        two directories. Prefer the new proposer-owned location; fall back to
        the legacy ``memory/`` location for pre-S2c run-dirs (so --continue
        keeps its findings). A fresh run writes the new location."""
        new = run_dir / "proposer" / "findings.jsonl"
        legacy = run_dir / "memory" / "findings.jsonl"
        if new.is_file():
            return new
        if legacy.is_file():
            return legacy
        return new

    # --- Reads ------------------------------------------------------------

    def load_all(self) -> dict[str, Finding]:
        """Return the current state of every finding, keyed by id.

        Later records override earlier ones with the same id (append-only
        update semantics). A missing file returns an empty dict.
        """
        if not self.path.is_file():
            return {}
        state: dict[str, Finding] = {}
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    # Torn last line from a crashed writer: skip.
                    continue
                if not isinstance(data, dict) or "id" not in data:
                    continue
                state[str(data["id"])] = Finding.from_dict(data)
        return state

    def get(self, finding_id: str) -> Finding | None:
        return self.load_all().get(finding_id)

    def exists(self, finding_id: str) -> bool:
        return finding_id in self.load_all()

    # --- Writes -----------------------------------------------------------

    def append(self, finding: Finding) -> None:
        """Persist one Finding record (create or update)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(finding.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def allocate_next_id(self) -> str:
        """Return the next available ``F-NNN`` id. Zero-padded to 3 digits,
        but grows as needed for larger runs."""
        highest = 0
        for existing in self.load_all():
            match = _FINDING_ID_RE.match(existing)
            if match is not None:
                highest = max(highest, int(match.group(1)))
        width = max(3, len(str(highest + 1)))
        return f"F-{highest + 1:0{width}d}"
