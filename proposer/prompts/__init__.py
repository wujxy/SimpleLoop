"""Proposer prompt loading.

The Scientist charter (``proposer.md``) lives in this package and travels with
it. The Host may override the charter for a run by passing a ``prompt_dir``
(via the lane manifest); otherwise the packaged default is used. This is the
S2b(i) cut of the ``simpleloop.prompts.load_semantic`` cross-dependency — the
proposer owns its own charter loading.
"""
from __future__ import annotations

from pathlib import Path

_DEFAULT_DIR = Path(__file__).resolve().parent


def load_semantic(role: str = "proposer",
                  prompt_dir: str | Path | None = None) -> str:
    """Read ``{prompt_dir}/{role}.md`` (or this package's ``{role}.md`` when
    ``prompt_dir`` is None). The proposer only loads ``"proposer"``."""
    root = Path(prompt_dir).resolve() if prompt_dir else _DEFAULT_DIR
    return (root / f"{role}.md").read_text(encoding="utf-8")
