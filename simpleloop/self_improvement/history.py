"""Versioned prompt snapshots and crash-recoverable supervisor state."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..prompts import PROMPT_NAMES, load_semantic
from .gate import OptimizerReport, identity_core


class PromptHistory:
    def __init__(self, prompt_dir: str | Path, history_dir: str | Path):
        self.prompt_dir = Path(prompt_dir)
        self.history_dir = Path(history_dir)
        self.state_path = self.history_dir / "state.yaml"
        self.events_path = self.history_dir / "events.jsonl"

    @property
    def state(self) -> dict:
        if not self.state_path.exists():
            return {}
        return yaml.safe_load(self.state_path.read_text(encoding="utf-8")) or {}

    def initialize(self) -> str:
        existing = self.state.get("active_version")
        if existing:
            self._normalize_active_set()
            return str(existing)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        orphan = self.history_dir / "v000"
        if orphan.exists():
            shutil.rmtree(orphan)
        for role in PROMPT_NAMES:
            _atomic_text(
                self.prompt_dir / f"{role}.md", load_semantic(role) + "\n",
            )
        self._write_snapshot("v000", manifest={
            "version": "v000", "parent": None, "trigger_round": None,
            "created_at": _stamp(), "changed": list(PROMPT_NAMES),
            "diagnosis": "identity-internalized initial prompt system",
            "evidence": [],
        })
        self._write_state({"active_version": "v000", "inflight": None})
        return "v000"

    def has_changes(self, version: str) -> bool:
        base = self.history_dir / version
        return any(
            (self.prompt_dir / f"{role}.md").read_bytes()
            != (base / f"{role}.md").read_bytes()
            for role in PROMPT_NAMES
        )

    def restore(self, version: str) -> None:
        source = self.history_dir / version
        expected = {f"{role}.md" for role in PROMPT_NAMES}
        for path in self.prompt_dir.iterdir():
            if path.name in expected and path.is_file() and not path.is_symlink():
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        for role in PROMPT_NAMES:
            target = self.prompt_dir / f"{role}.md"
            temporary = target.with_suffix(".restore")
            shutil.copyfile(source / f"{role}.md", temporary)
            os.replace(temporary, target)
        self._normalize_active_set()

    def snapshot(self, trigger_round: int, report: OptimizerReport) -> str:
        state = self.state
        parent = state["active_version"]
        version = f"v{int(parent[1:]) + 1:03d}"
        changed = [
            f"{role}.md" for role in PROMPT_NAMES
            if (self.prompt_dir / f"{role}.md").read_bytes()
            != (self.history_dir / parent / f"{role}.md").read_bytes()
        ]
        self._write_snapshot(version, manifest={
            "version": version, "parent": parent,
            "trigger_round": trigger_round, "created_at": _stamp(),
            "changed": changed, "diagnosis": report.diagnosis,
            "evidence": report.evidence,
        })
        state["active_version"] = version
        self._write_state(state)
        return version

    def mark_inflight(self, run_id: str, trigger_round: int) -> bool:
        key = (run_id, trigger_round)
        if any((e.get("run_id"), e.get("trigger_round")) == key for e in self.events()):
            return False
        state = self.state
        if state.get("inflight"):
            return False
        state["inflight"] = {
            "run_id": run_id, "trigger_round": trigger_round,
            "parent": state["active_version"],
        }
        self._write_state(state)
        return True

    def recover_inflight(self) -> dict | None:
        state = self.state
        inflight = state.get("inflight")
        if not inflight:
            return None
        key = (inflight["run_id"], inflight["trigger_round"])
        completed = any(
            (event.get("run_id"), event.get("trigger_round")) == key
            and event.get("status") in {"accepted", "no_change", "rejected"}
            for event in self.events()
        )
        if completed:
            state["inflight"] = None
            self._write_state(state)
            return inflight
        parent = str(inflight["parent"])
        self.restore(parent)
        self._discard_versions_after(parent)
        if not any(
            (event.get("run_id"), event.get("trigger_round")) == key
            and event.get("status") == "interrupted"
            for event in self.events()
        ):
            self.record_event({**inflight, "status": "interrupted"})
        state["active_version"] = parent
        state["inflight"] = None
        self._write_state(state)
        return inflight

    def finish_trigger(self, status: str, result_version: str | None = None,
                       error: str | None = None) -> None:
        state = self.state
        inflight = state.get("inflight")
        if not inflight:
            raise ValueError("no prompt optimizer trigger is in flight")
        event = {**inflight, "status": status}
        if result_version:
            event["result_version"] = result_version
        if error:
            event["error"] = error
        self.record_event(event)
        state["inflight"] = None
        self._write_state(state)

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines() if line]

    def record_event(self, event: dict) -> None:
        events = self.events()
        events.append({**event, "created_at": _stamp()})
        _atomic_text(
            self.events_path,
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
        )

    def _write_snapshot(self, version: str, manifest: dict) -> None:
        target = self.history_dir / version
        target.mkdir(parents=True, exist_ok=False)
        for role in PROMPT_NAMES:
            shutil.copyfile(self.prompt_dir / f"{role}.md", target / f"{role}.md")
        (target / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _discard_versions_after(self, version: str) -> None:
        current = int(version[1:])
        for path in self.history_dir.iterdir():
            if (
                path.is_dir()
                and path.name.startswith("v")
                and path.name[1:].isdigit()
                and int(path.name[1:]) > current
            ):
                shutil.rmtree(path)

    def _write_state(self, state: dict) -> None:
        _atomic_text(
            self.state_path,
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False),
        )

    def _normalize_active_set(self) -> None:
        legacy = self.prompt_dir / "judger.md"
        if legacy.exists() or legacy.is_symlink():
            legacy.unlink()
        meta = self.prompt_dir / "meta_optimizer.md"
        current = meta.read_text(encoding="utf-8")
        old_core = identity_core(current)
        new_core = identity_core(load_semantic("meta_optimizer"))
        if old_core != new_core:
            _atomic_text(meta, current.replace(old_core, new_core, 1))


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()
