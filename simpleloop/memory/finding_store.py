"""Append-only Finding Archive.

Findings live at ``<run_dir>/memory/findings.jsonl``. Every mutation (create,
link experiments, update stats, change state) appends a new full record; the
current state of finding ``F-NNN`` is the last record with that id. This
mirrors ``history.jsonl``'s audit-friendly semantics — nothing is ever silently
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
        self.path = self.run_dir / "memory" / "findings.jsonl"

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
