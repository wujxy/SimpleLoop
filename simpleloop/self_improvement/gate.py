"""Static boundaries for an optimizer-edited semantic prompt set."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..prompts import PROMPT_NAMES, load_semantic


_CORE_BEGIN = "<!-- META_IDENTITY_CORE_BEGIN -->"
_CORE_END = "<!-- META_IDENTITY_CORE_END -->"
MAX_PROMPT_CHARS = 30000


def identity_core(text: str) -> str:
    start = text.find(_CORE_BEGIN)
    end = text.find(_CORE_END, start + len(_CORE_BEGIN))
    if start < 0 or end < 0:
        raise ValueError("META_IDENTITY_CORE markers are missing")
    return text[start:end + len(_CORE_END)]


@dataclass(frozen=True)
class OptimizerReport:
    diagnosis: str
    evidence: list[str]

    @classmethod
    def load(cls, path: str | Path) -> "OptimizerReport":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        fields = {"diagnosis", "evidence"}
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError(
                "optimizer_report.yaml must contain exactly diagnosis and evidence"
            )
        if not isinstance(data["diagnosis"], str) or not data["diagnosis"].strip():
            raise ValueError("optimizer report diagnosis must be non-empty")
        evidence = data["evidence"]
        if not isinstance(evidence, list) or not all(isinstance(v, str) for v in evidence):
            raise ValueError("optimizer report evidence must be a list of strings")
        return cls(
            diagnosis=data["diagnosis"].strip(), evidence=evidence,
        )


class PromptGate:
    def __init__(self):
        self.expected_core = identity_core(load_semantic("meta_optimizer"))

    def check(self, prompt_dir: str | Path) -> list[str]:
        root = Path(prompt_dir)
        errors: list[str] = []
        allowed = {f"{name}.md" for name in PROMPT_NAMES} | {"optimizer_report.yaml"}
        actual = {path.name for path in root.iterdir()} if root.is_dir() else set()
        extra = actual - allowed
        if extra:
            errors.append(f"unexpected prompt files: {sorted(extra)}")
        for name in PROMPT_NAMES:
            path = root / f"{name}.md"
            if path.is_symlink():
                errors.append(f"{path.name} must not be a symbolic link")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"{path.name} is not readable UTF-8: {exc}")
                continue
            if len(text) > MAX_PROMPT_CHARS:
                errors.append(
                    f"{path.name} exceeds {MAX_PROMPT_CHARS} characters"
                )
            if name == "meta_optimizer":
                try:
                    if identity_core(text) != self.expected_core:
                        errors.append("META_IDENTITY_CORE changed")
                except ValueError as exc:
                    errors.append(str(exc))
        return errors
