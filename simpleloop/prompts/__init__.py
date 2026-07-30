"""Load evolvable role semantics from the active prompt directory."""
from __future__ import annotations

from pathlib import Path


PROMPT_NAMES = ("proposer", "executor", "judger", "meta_optimizer")


def load_semantic(
    role: str,
    prompt_dir: str | Path | None = None,
) -> str:
    if role not in PROMPT_NAMES:
        raise ValueError(f"unknown prompt role: {role}")
    root = Path(prompt_dir) if prompt_dir is not None else Path(__file__).parent
    return (root / f"{role}.md").read_text(encoding="utf-8").strip()
