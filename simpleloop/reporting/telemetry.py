"""Minimal persisted telemetry for progress plotting."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable


_CACHE_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")
_TOKEN_KEYS = ("input_tokens", "output_tokens", *_CACHE_KEYS)


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def processed_tokens(usage: object) -> int:
    """Sum every valid Claude token field; missing values contribute zero."""
    if not isinstance(usage, dict):
        return 0
    return sum(
        value
        for key in _TOKEN_KEYS
        if (value := _non_negative_int(usage.get(key))) is not None
    )


def _non_negative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def load_plot_context(run_dir: str | Path) -> dict:
    """Rebuild a live run's plot_context from its persisted telemetry.json.

    Offline entry (`simpleloop plot`): the baseline metrics/telemetry the loop
    passes to the plotter in-memory are persisted per round, so a finished (or
    still-running) run can be re-plotted without re-running anything. Missing
    or unreadable telemetry degrades to an empty context (plots still render,
    just without the baseline point).
    """
    path = Path(run_dir) / "telemetry.json"
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[telemetry] warning: could not read {path}: {exc}", flush=True)
        return {}
    if not isinstance(state, dict):
        return {}
    baseline_metrics = state.get("baseline_metrics")
    baseline_telemetry = state.get("baseline_telemetry")
    return {
        "baseline_metrics": (
            dict(baseline_metrics) if isinstance(baseline_metrics, dict) else {}
        ),
        "baseline_telemetry": (
            dict(baseline_telemetry)
            if isinstance(baseline_telemetry, dict)
            else {}
        ),
    }


class RunTelemetry:
    """Track the exact run-level values needed by progress plots."""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        resume: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.path = Path(run_dir) / "telemetry.json"
        self._clock = clock
        self._lock = threading.RLock()
        self._segment_start = clock()
        state = self._load() if resume else {}

        default_worktime = None if resume else 0.0
        loaded_worktime = state.get("worktime_seconds", default_worktime)
        self._worktime = _non_negative_number(loaded_worktime)
        self._tokens = {
            key: _non_negative_int(state.get(key)) or 0
            for key in _TOKEN_KEYS
        }
        baseline_metrics = state.get("baseline_metrics")
        baseline_telemetry = state.get("baseline_telemetry")
        self._baseline_metrics = (
            dict(baseline_metrics) if isinstance(baseline_metrics, dict) else None
        )
        self._baseline_telemetry = (
            dict(baseline_telemetry)
            if isinstance(baseline_telemetry, dict)
            else None
        )

    def record_usage(self, usage: object) -> None:
        with self._lock:
            if isinstance(usage, dict):
                for key in _TOKEN_KEYS:
                    count = _non_negative_int(usage.get(key))
                    if count is not None:
                        self._tokens[key] += count
            self._persist_locked()

    def set_baseline(self, metrics: dict) -> None:
        """Fix the initial baseline once; continue-mode calls cannot replace it."""
        with self._lock:
            if self._baseline_metrics is not None:
                return
            self._baseline_metrics = dict(metrics)
            self._baseline_telemetry = self._snapshot_locked()
            self._persist_locked(self._baseline_telemetry)

    def snapshot(self, *, persist: bool = False) -> dict:
        with self._lock:
            result = self._snapshot_locked()
            if persist:
                self._worktime = result["worktime_seconds"]
                self._segment_start = self._clock()
                self._persist_locked(result)
            return result

    def plot_context(self) -> dict:
        with self._lock:
            return {
                "baseline_metrics": dict(self._baseline_metrics or {}),
                "baseline_telemetry": dict(self._baseline_telemetry or {}),
            }

    def _snapshot_locked(self) -> dict:
        worktime = self._worktime
        if worktime is not None:
            worktime += max(0.0, self._clock() - self._segment_start)
        token_counts = dict(self._tokens)
        return {
            "worktime_seconds": worktime,
            **token_counts,
            "processed_tokens": sum(token_counts.values()),
        }

    def _state_locked(self, snapshot: dict | None = None) -> dict:
        current = snapshot or self._snapshot_locked()
        return {
            "worktime_seconds": current["worktime_seconds"],
            **{key: current[key] for key in _TOKEN_KEYS},
            "processed_tokens": current["processed_tokens"],
            "baseline_metrics": self._baseline_metrics,
            "baseline_telemetry": self._baseline_telemetry,
        }

    def _persist_locked(self, snapshot: dict | None = None) -> None:
        temporary = self.path.with_name(".telemetry.tmp.json")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(self._state_locked(snapshot), ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except Exception as exc:
            print(f"[telemetry] warning: could not update {self.path}: {exc}",
                  flush=True)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("root must be an object")
            return loaded
        except Exception as exc:
            print(f"[telemetry] warning: could not read {self.path}: {exc}",
                  flush=True)
            return {}
