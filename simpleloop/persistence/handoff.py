"""Atomic handoff-file writes for pipeline observability.

Each pipeline stage (proposer, executor, eval) writes a JSON file to
``run_dir/handoffs/r{N}/`` the moment it completes — not at round-end.
These files are non-authoritative (``history.jsonl`` remains the source of
truth); they exist so any stage's output can be inspected mid-round or
reproduced after a crash.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def write_handoff(run_dir: Path | None, round_id: int, name: str,
                  data: dict) -> None:
    """Atomically write a handoff JSON file.

    ``name`` should include the ``.json`` suffix, e.g. ``"proposals.json"``
    or ``"r0-c0.executor.json"``. The file is written to
    ``run_dir/handoffs/r{round_id}/{name}`` via tmp + ``os.replace`` so a
    reader never sees a partial write.

    If ``run_dir`` is None (test mode without a real run directory), the
    call is a no-op.
    """
    if run_dir is None:
        return
    d = Path(run_dir) / "handoffs" / f"r{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
