"""Static boundaries for an optimizer-edited semantic prompt set."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..prompts import PROMPT_NAMES, load_semantic


_CORE_BEGIN = "<!-- META_IDENTITY_CORE_BEGIN -->"
_CORE_END = "<!-- META_IDENTITY_CORE_END -->"


def identity_core(text: str) -> str:
    start = text.find(_CORE_BEGIN)
    end = text.find(_CORE_END, start + len(_CORE_BEGIN))
    if start < 0 or end < 0:
        raise ValueError("META_IDENTITY_CORE markers are missing")
    return text[start:end + len(_CORE_END)]


@dataclass(frozen=True)
class OptimizerReport:
    status: str
    diagnosis: str
    evidence: list[str]
    intent: str

    @classmethod
    def load(cls, path: str | Path) -> "OptimizerReport":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        fields = {"status", "diagnosis", "evidence", "intent"}
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError(
                "optimizer_report.yaml must contain exactly status, diagnosis, "
                "evidence, and intent"
            )
        if data["status"] not in {"changed", "no_change"}:
            raise ValueError("optimizer report status must be changed or no_change")
        if not isinstance(data["diagnosis"], str) or not data["diagnosis"].strip():
            raise ValueError("optimizer report diagnosis must be non-empty")
        evidence = data["evidence"]
        if not isinstance(evidence, list) or not all(isinstance(v, str) for v in evidence):
            raise ValueError("optimizer report evidence must be a list of strings")
        if not isinstance(data["intent"], str):
            raise ValueError("optimizer report intent must be a string")
        return cls(
            status=data["status"], diagnosis=data["diagnosis"].strip(),
            evidence=evidence, intent=data["intent"].strip(),
        )


class PromptGate:
    def __init__(self, max_prompt_chars: int):
        self.max_prompt_chars = max_prompt_chars
        self.expected_core = identity_core(load_semantic("meta_optimizer"))

    def check(
        self,
        prompt_dir: str | Path,
        report: OptimizerReport,
        *,
        changed: bool,
    ) -> list[str]:
        root = Path(prompt_dir)
        errors: list[str] = []
        allowed = {f"{name}.md" for name in PROMPT_NAMES} | {"optimizer_report.yaml"}
        actual = {path.name for path in root.iterdir()} if root.is_dir() else set()
        extra = actual - allowed
        if extra:
            errors.append(f"unexpected prompt files: {sorted(extra)}")
        for name in PROMPT_NAMES:
            path = root / f"{name}.md"
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"{path.name} is not readable UTF-8: {exc}")
                continue
            if len(text) > self.max_prompt_chars:
                errors.append(f"{path.name} exceeds {self.max_prompt_chars} characters")
            if name == "meta_optimizer":
                try:
                    if identity_core(text) != self.expected_core:
                        errors.append("META_IDENTITY_CORE changed")
                except ValueError as exc:
                    errors.append(str(exc))
        if changed and report.status == "no_change":
            errors.append("optimizer reported no_change but prompt files changed")
        if not changed and report.status == "changed":
            errors.append("optimizer reported changed but prompt files did not change")
        return errors
