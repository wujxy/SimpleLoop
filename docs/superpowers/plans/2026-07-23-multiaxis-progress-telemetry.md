# SimpleLoop Multi-Axis Progress Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the minimum telemetry needed to render one 3×3 progress overview and nine detail plots against round, active worktime, and processed Claude tokens, while removing future `final_report.md` generation.

**Architecture:** Add one thread-safe `RunTelemetry` object shared by the three Agents and the loop. It persists only the fixed baseline, cumulative active seconds, cumulative processed tokens, and baseline snapshot; history records receive completion snapshots. Refactor plotting around one pure observation series and one generic panel renderer, then publish `progress.png` plus nine stable detail filenames.

**Tech Stack:** Python 3.9+, standard-library `json`/`threading`/`time`, Matplotlib `Agg`, pytest, JSONL history.

## Global Constraints

- No new configuration keys or formula language.
- Ratio is always `current objective / fixed initial baseline objective`.
- Ratio label is exactly `<key> ratio (vs baseline)`.
- Worktime counts only active SimpleLoop process time and resumes from the last persisted active duration.
- Processed tokens equal `input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens`.
- Missing required usage makes the cumulative token coordinate `None` from that call onward; never estimate it.
- `progress.png` is the 3×3 overview; also produce the nine confirmed standalone PNGs.
- Monitoring failures warn and do not alter optimization selection, acceptance, or lineage.
- Do not backfill or delete artifacts in old runs.
- Do not generate `final_report.md` for new or continued runs.
- Keep this MVP: no UTC audit fields, schema-version framework, per-call ledger, token-category persistence, pricing, dynamic unit selection, dashboard, or migration command.

---

## File Map

- Create `simpleloop/telemetry.py`: minimal run-level state, usage summation, monotonic active clock, atomic persistence.
- Create `simpleloop/tests/test_telemetry.py`: telemetry unit tests.
- Modify `simpleloop/agent.py`: extract Claude usage and notify an optional observer without changing `run_json`/`run_text`.
- Modify `simpleloop/tests/test_parallel_candidates.py`: Agent envelope and observer tests.
- Modify `simpleloop/store.py`: persist optional telemetry snapshots; remove report generation.
- Modify `simpleloop/loop.py`: create the tracker, attach snapshots, preserve baseline across continue, refresh plots with baseline context.
- Modify `simpleloop/tests/test_parallel_candidates.py`: parallel candidate and generation snapshot tests.
- Modify `simpleloop/tests/test_plot.py`: serial/failure integration, nine-series, overview/detail rendering, legacy behavior.
- Modify `simpleloop/plot.py`: observation transformation and generic 3×3/detail rendering.
- Modify `simpleloop/tests/test_views_and_parse.py`: remove obsolete report test and retain Store persistence coverage.
- Modify `simpleloop/views.py`: remove the stale final-report comment only.
- Modify `README.md`: list telemetry/history/plot artifacts instead of `final_report.md`.

---

### Task 1: Minimal Run Telemetry

**Files:**
- Create: `simpleloop/telemetry.py`
- Create: `simpleloop/tests/test_telemetry.py`

**Interfaces:**
- Produces: `processed_tokens(usage: object) -> int | None`
- Produces: `RunTelemetry(run_dir: Path, *, resume: bool = False, clock: Callable[[], float] = time.monotonic)`
- Produces: `RunTelemetry.record_usage(usage: object) -> None`
- Produces: `RunTelemetry.set_baseline(metrics: dict) -> None`
- Produces: `RunTelemetry.snapshot(*, persist: bool = False) -> dict`
- Produces: `RunTelemetry.plot_context() -> dict`
- Persists: `run_dir/telemetry.json`

- [ ] **Step 1: Write failing token-sum and invalid-usage tests**

Add:

```python
from simpleloop.telemetry import processed_tokens


def test_processed_tokens_sums_claude_usage_fields():
    assert processed_tokens({
        "input_tokens": 10,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30,
        "output_tokens": 5,
    }) == 65


def test_processed_tokens_treats_missing_cache_fields_as_zero():
    assert processed_tokens({"input_tokens": 10, "output_tokens": 5}) == 15


@pytest.mark.parametrize("usage", [
    None,
    {},
    {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": 1},
])
def test_processed_tokens_rejects_incomplete_or_invalid_usage(usage):
    assert processed_tokens(usage) is None
```

Input and output are required non-negative integers. Cache fields are optional
because Claude omits zero-valued cache categories in some envelopes.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_telemetry.py -q
```

Expected: collection fails because `simpleloop.telemetry` does not exist.

- [ ] **Step 3: Implement the token sum**

Add:

```python
_CACHE_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def processed_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = _non_negative_int(usage.get("input_tokens"))
    output_tokens = _non_negative_int(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    cache = 0
    for key in _CACHE_KEYS:
        value = _non_negative_int(usage.get(key, 0))
        if value is None:
            return None
        cache += value
    return input_tokens + output_tokens + cache
```

- [ ] **Step 4: Add failing fresh/resume/baseline tests**

Use a deterministic clock:

```python
class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def test_fresh_tracker_records_active_time_tokens_and_baseline(tmp_path):
    clock = Clock(10.0)
    tracker = RunTelemetry(tmp_path, clock=clock)
    clock.now = 15.0
    tracker.record_usage({"input_tokens": 4, "output_tokens": 1})
    tracker.set_baseline({"SPEED_MS": 100.0})

    assert tracker.snapshot() == {
        "worktime_seconds": 5.0,
        "processed_tokens": 5,
    }
    assert tracker.plot_context() == {
        "baseline_metrics": {"SPEED_MS": 100.0},
        "baseline_telemetry": {
            "worktime_seconds": 5.0,
            "processed_tokens": 5,
        },
    }


def test_missing_usage_makes_tokens_permanently_unavailable(tmp_path):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    tracker.record_usage(None)
    tracker.record_usage({"input_tokens": 4, "output_tokens": 1})
    assert tracker.snapshot()["processed_tokens"] is None


def test_resume_adds_only_new_active_segment_and_keeps_baseline(tmp_path):
    first_clock = Clock(0.0)
    first = RunTelemetry(tmp_path, clock=first_clock)
    first_clock.now = 8.0
    first.set_baseline({"SPEED_MS": 100.0})
    first.snapshot(persist=True)

    resumed_clock = Clock(1000.0)
    resumed = RunTelemetry(tmp_path, resume=True, clock=resumed_clock)
    resumed_clock.now = 1003.0
    resumed.set_baseline({"SPEED_MS": 999.0})

    assert resumed.snapshot()["worktime_seconds"] == 11.0
    assert resumed.plot_context()["baseline_metrics"] == {"SPEED_MS": 100.0}
```

- [ ] **Step 5: Add failing persistence and thread-safety tests**

```python
def test_persisted_state_is_valid_json(tmp_path):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    tracker.snapshot(persist=True)
    state = json.loads((tmp_path / "telemetry.json").read_text())
    assert state["worktime_seconds"] == 0.0
    assert state["processed_tokens"] == 0
    assert set(state) == {
        "worktime_seconds",
        "processed_tokens",
        "baseline_metrics",
        "baseline_telemetry",
    }
    assert not (tmp_path / ".telemetry.tmp.json").exists()


def test_concurrent_usage_is_counted_once(tmp_path):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    usage = {"input_tokens": 2, "output_tokens": 1}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(tracker.record_usage, [usage] * 100))
    assert tracker.snapshot()["processed_tokens"] == 300


def test_persistence_failure_warns_without_losing_in_memory_state(
    monkeypatch, tmp_path, capsys,
):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    monkeypatch.setattr("simpleloop.telemetry.os.replace",
                        lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    tracker.record_usage({"input_tokens": 2, "output_tokens": 1})
    assert tracker.snapshot()["processed_tokens"] == 3
    assert "[telemetry] warning:" in capsys.readouterr().out
```

- [ ] **Step 6: Implement `RunTelemetry` minimally**

Use one `threading.RLock`, one segment start, and four persisted fields:

```python
class RunTelemetry:
    def __init__(self, run_dir, *, resume=False, clock=time.monotonic):
        self.path = Path(run_dir) / "telemetry.json"
        self._clock = clock
        self._lock = threading.RLock()
        self._segment_start = clock()
        state = self._load() if resume else {}
        self._worktime = state.get("worktime_seconds", 0.0 if not resume else None)
        self._tokens = state.get("processed_tokens", 0 if not resume else None)
        self._baseline_metrics = state.get("baseline_metrics")
        self._baseline_telemetry = state.get("baseline_telemetry")

    def record_usage(self, usage):
        with self._lock:
            count = processed_tokens(usage)
            self._tokens = (
                self._tokens + count
                if self._tokens is not None and count is not None
                else None
            )
            self._persist_locked()

    def set_baseline(self, metrics):
        with self._lock:
            if self._baseline_metrics is None:
                self._baseline_metrics = dict(metrics)
                self._baseline_telemetry = self._snapshot_locked()
                self._persist_locked()

    def snapshot(self, *, persist=False):
        with self._lock:
            result = self._snapshot_locked()
            if persist:
                self._worktime = result["worktime_seconds"]
                self._segment_start = self._clock()
                self._persist_locked()
            return result

    def plot_context(self):
        with self._lock:
            return {
                "baseline_metrics": dict(self._baseline_metrics or {}),
                "baseline_telemetry": dict(self._baseline_telemetry or {}),
            }
```

`_snapshot_locked()` adds `clock() - _segment_start` only when `_worktime` is
numeric. `_persist_locked()` serializes a fresh `_snapshot_locked()` plus the two
baseline fields to `.telemetry.tmp.json` and calls `os.replace`. This ensures
each completed Agent call persists current worktime as well as tokens. `_load()` and persistence errors
print `[telemetry] warning: ...`; a missing/corrupt resume file yields
`worktime_seconds=None`, `processed_tokens=None`, and no baseline. Do not add a
version field or recovery/migration framework.

- [ ] **Step 7: Run telemetry tests and verify GREEN**

Run:

```bash
python -m pytest simpleloop/tests/test_telemetry.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add simpleloop/telemetry.py simpleloop/tests/test_telemetry.py
git commit -m "feat: track minimal run telemetry"
```

---

### Task 2: Claude Usage Observer

**Files:**
- Modify: `simpleloop/agent.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: `usage_observer: Callable[[object], None] | None`
- Produces: `AgentResult(text: str, data: dict, usage: object = None)`
- Produces: `_decode_output(stdout: str) -> AgentResult`
- Preserves: `Agent.run_json(...) -> dict`
- Preserves: `Agent.run_text(...) -> str`

- [ ] **Step 1: Write failing Claude-envelope extraction tests**

```python
from simpleloop.agent import _decode_output


def test_decode_output_extracts_structured_result_and_usage():
    result = _decode_output(json.dumps({
        "result": "fallback",
        "structured_output": {"score": 0.8},
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
        },
    }))
    assert result.text == "fallback"
    assert result.data == {"score": 0.8}
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
    }


def test_decode_output_keeps_legacy_plain_text_and_missing_usage():
    result = _decode_output('{"score": 0.8}')
    assert result.text == '{"score": 0.8}'
    assert result.data == {}
    assert result.usage is None
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  -k "decode_output" -q
```

Expected: import failure because `_decode_output` does not exist.

- [ ] **Step 3: Extract the existing envelope parsing into `_decode_output`**

Extend the dataclass compatibly:

```python
@dataclass
class AgentResult:
    text: str
    data: dict
    usage: object = None
```

Move the existing JSON-envelope logic from `_run` into `_decode_output`.
Only `result`, `structured_output`, and `usage` are read. Do not introduce a
general Claude response model.

- [ ] **Step 4: Write failing observer tests**

```python
def test_agent_notifies_usage_observer_without_changing_public_result():
    seen = []
    agent = Agent(usage_observer=seen.append)
    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "proposer")
    assert seen == [{"input_tokens": 1, "output_tokens": 2}]


def test_usage_observer_failure_is_nonfatal(capsys):
    def fail(_usage):
        raise OSError("state unavailable")

    agent = Agent(usage_observer=fail)
    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "judger")
    assert "[telemetry] warning:" in capsys.readouterr().out
```

- [ ] **Step 5: Implement observer notification at the single `_run` boundary**

Add `usage_observer` to `Agent.__init__` and:

```python
def _notify_usage(self, usage: object, label: str) -> None:
    if self.usage_observer is None:
        return
    try:
        self.usage_observer(usage)
    except Exception as exc:
        print(f"[telemetry] warning: {label} usage was not recorded: {exc}",
              flush=True)
```

After a subprocess completes, decode stdout once, notify with
`result.usage`, then preserve the existing return/error behavior. On timeout,
notify `None` before raising. This makes unknown usage explicit without
changing proposer/executor/judger APIs. Do not store per-call labels or
durations.

- [ ] **Step 6: Run Agent and role-boundary tests**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_views_and_parse.py \
  -k "agent or proposer or judger" -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add simpleloop/agent.py simpleloop/tests/test_parallel_candidates.py
git commit -m "feat: observe claude token usage"
```

---

### Task 3: Persist Completion Snapshots In History

**Files:**
- Modify: `simpleloop/store.py`
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`
- Modify: `simpleloop/tests/test_plot.py`

**Interfaces:**
- Consumes: `RunTelemetry`
- Extends: `Store.append(..., telemetry: dict | None = None) -> None`
- Extends: `Store.append_generation(..., telemetry: dict | None = None) -> None`
- Extends: `_run_candidates(..., telemetry: RunTelemetry) -> list[dict]`
- Extends: `_record_failure(..., telemetry_tracker: RunTelemetry | None = None) -> None`

- [ ] **Step 1: Write failing Store snapshot tests**

```python
def test_store_persists_serial_telemetry(tmp_path):
    store = Store(tmp_path)
    snapshot = {"worktime_seconds": 12.5, "processed_tokens": 100}
    store.append(0, "p", "sha", 0.5, "feedback", telemetry=snapshot)
    assert store.history()[0]["telemetry"] == snapshot


def test_store_persists_candidate_and_generation_telemetry(tmp_path):
    store = Store(tmp_path)
    candidate_snapshot = {"worktime_seconds": 2.0, "processed_tokens": 10}
    generation_snapshot = {"worktime_seconds": 3.0, "processed_tokens": 12}
    store.append_generation(
        0,
        parent_sha="base",
        selected_candidate=0,
        selected_sha="sha",
        candidates=[{
            "candidate": 0,
            "sha": "sha",
            "telemetry": candidate_snapshot,
        }],
        telemetry=generation_snapshot,
    )
    row = store.history()[0]
    assert row["telemetry"] == generation_snapshot
    assert row["candidates"][0]["telemetry"] == candidate_snapshot
```

- [ ] **Step 2: Run Store tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_views_and_parse.py \
  -k "telemetry" -q
```

Expected: `TypeError` because Store methods do not accept `telemetry`.

- [ ] **Step 3: Add only the two history fields**

In `Store.append`, write:

```python
"telemetry": dict(telemetry or {}),
```

In `Store.append_generation`, copy each candidate's `telemetry` and write the
generation `telemetry`. Do not add timestamps, role totals, or a second history
file.

- [ ] **Step 4: Write failing parallel completion-coordinate test**

Use a fake tracker whose snapshots are distinguishable:

```python
class SnapshotTracker:
    def __init__(self):
        self.value = 0

    def snapshot(self, *, persist=False):
        self.value += 1
        return {
            "worktime_seconds": float(self.value),
            "processed_tokens": self.value * 10,
        }


def test_run_candidates_attaches_snapshot_after_each_worker(monkeypatch):
    monkeypatch.setattr(
        loop_mod,
        "_run_one_candidate",
        lambda candidate_id, proposal, *_args: {
            "candidate": candidate_id,
            "proposal": proposal.proposal,
            "score": 0.5,
        },
    )
    tracker = SnapshotTracker()
    candidates = _run_candidates(
        proposals, 0, "base", {"max_workers": 1}, workspace,
        executor_agent, judger_agent, {}, {}, None, "", tracker,
    )
    assert candidates[0]["telemetry"] == {
        "worktime_seconds": 1.0,
        "processed_tokens": 10,
    }
```

Add the same assertion to the existing parallel worker-failure test so every
returned candidate, including a local failure, has a snapshot.

- [ ] **Step 5: Attach candidate snapshots in `_run_candidates`**

Add `telemetry` as the last parameter. In serial mode replace the list
comprehension with a loop; immediately after `_run_one_candidate` returns:

```python
candidate["telemetry"] = telemetry.snapshot(persist=True)
results.append(candidate)
```

In parallel mode attach the snapshot immediately after `future.result()` or
after `_candidate_failure(...)` is created. The coordinator takes snapshots
after workers have completed, so `_run_one_candidate` remains unaware of
telemetry.

- [ ] **Step 6: Write failing fresh-baseline and continue-preservation tests**

Patch `RunTelemetry` with a capturing fake in loop integration tests. Verify:

```python
assert created_tracker.resume is False
assert created_tracker.baselines == [{"SPEED_MS": 100.0}]
assert all(agent.usage_observer == created_tracker.record_usage
           for agent in created_agents)
```

For `continue_run=True`, seed the fake with baseline
`{"SPEED_MS": 100.0}`, make `_eval_baseline` return
`{"SPEED_MS": 999.0}`, and assert `set_baseline` does not replace the persisted
baseline. The real `RunTelemetry.set_baseline` test from Task 1 enforces the
replacement rule; the loop test only proves it uses the tracker.

- [ ] **Step 7: Integrate one tracker through `run`**

Create it immediately after `run_dir_path.mkdir(...)`:

```python
telemetry = RunTelemetry(run_dir_path, resume=continue_run)
```

Pass `telemetry.record_usage` to all three Agents. After the fresh-run baseline
eval call:

```python
telemetry.set_baseline(baseline_metrics)
```

Do not call `set_baseline` with continue-mode re-evaluation metrics. Pass the
tracker into `_run_candidates`, `_record_failure`, and serial append paths.
At generation completion:

```python
generation_telemetry = telemetry.snapshot(persist=True)
store.append_generation(..., telemetry=generation_telemetry)
```

At serial completion or failure use one `snapshot(persist=True)` in the record.
Do not add phase timers; the shared monotonic clock already measures active
process time.

- [ ] **Step 8: Run history and loop integration tests**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_plot.py \
  simpleloop/tests/test_views_and_parse.py -q
```

Expected: all tests pass. Keep `_refresh_progress_plot` on the existing
`write_progress_png` boundary until Task 4 implements the replacement API.

- [ ] **Step 9: Commit**

```bash
git add simpleloop/store.py simpleloop/loop.py \
  simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_plot.py
git commit -m "feat: persist progress telemetry snapshots"
```

---

### Task 4: Render The 3×3 Overview And Nine Detail Plots

**Files:**
- Modify: `simpleloop/plot.py`
- Modify: `simpleloop/tests/test_plot.py`

**Interfaces:**
- Produces: `Observation`
- Produces: `PlotSeries`
- Produces: `build_series(history: list[dict], metrics_schema: dict | None, plot_context: dict | None = None) -> PlotSeries`
- Produces: `write_progress_pngs(run_dir, history, metrics_schema, plot_context) -> list[Path]`
- Internal: `_render_panel(axis, series, y_kind: str, x_kind: str) -> None`
- Extends: `_refresh_progress_plot(store: Store, plot_context: dict | None = None) -> None`

- [ ] **Step 1: Replace the old shape assertions with failing observation tests**

Define expected observations from one parallel generation:

```python
series = build_series(
    history=[{
        "round": 0,
        "selected_candidate": 1,
        "selected_sha": "winner",
        "telemetry": {
            "worktime_seconds": 30.0,
            "processed_tokens": 300,
        },
        "candidates": [
            {
                "candidate": 0,
                "score": 0.4,
                "metrics": {"SPEED_MS": 120.0},
                "telemetry": {
                    "worktime_seconds": 20.0,
                    "processed_tokens": 200,
                },
            },
            {
                "candidate": 1,
                "score": 0.8,
                "metrics": {"SPEED_MS": 80.0},
                "telemetry": {
                    "worktime_seconds": 25.0,
                    "processed_tokens": 250,
                },
            },
        ],
    }],
    metrics_schema=SCHEMA,
    plot_context={
        "baseline_metrics": {"SPEED_MS": 100.0},
        "baseline_telemetry": {
            "worktime_seconds": 10.0,
            "processed_tokens": 0,
        },
    },
)

assert series.candidates[1].round == 1
assert series.candidates[1].worktime_hours == 25.0 / 3600.0
assert series.candidates[1].processed_tokens == 250
assert series.candidates[1].objective == 80.0
assert series.candidates[1].ratio == 0.8
assert series.candidates[1].selected is True
assert series.baseline.objective == 100.0
assert series.baseline.ratio == 1.0
assert series.incumbents[-1].objective == 80.0
assert series.incumbents[-1].worktime_hours == 30.0 / 3600.0
```

- [ ] **Step 2: Add failing invalid-ratio and legacy tests**

Cover baseline `0`, `NaN`, Boolean, and missing. In every case objective points
remain and ratio is `None`. Retain a legacy serial-history test proving
score/objective vs round still have data while worktime/tokens and baseline are
`None`.

- [ ] **Step 3: Run series tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py -k "build_series" -q
```

Expected: failures because the old `PlotSeries` has no observations, ratio, or
resource coordinates.

- [ ] **Step 4: Implement the minimal observation model**

Use two dataclasses:

```python
@dataclass
class Observation:
    round: float
    worktime_hours: float | None
    processed_tokens: int | None
    score: float | None
    objective: float | None
    ratio: float | None
    selected: bool = False


@dataclass
class PlotSeries:
    candidates: list[Observation]
    incumbents: list[Observation]
    baseline: Observation | None
    objective_key: str | None
    lower_is_better: bool | None
    rounds: list[int]
```

Keep one `_number` validator that rejects Booleans and non-finite values. Keep
one `_observation(...)` helper. Do not create a metric registry, dataframe, or
plugin interface.

For parallel history, candidate coordinates come from candidate telemetry and
incumbent coordinates come from generation telemetry. For serial history, both
come from the record telemetry. Start the incumbent from the fixed baseline,
carry it through no-winner rounds, and calculate ratio only as
`objective / baseline`.

- [ ] **Step 5: Add failing output-manifest and PNG tests**

Declare the exact filenames in the test:

```python
DETAIL_OUTPUTS = {
    "progress-score-vs-round.png",
    "progress-score-vs-worktime.png",
    "progress-score-vs-tokens.png",
    "progress-objective-vs-round.png",
    "progress-objective-vs-worktime.png",
    "progress-objective-vs-tokens.png",
    "progress-objective-ratio-vs-round.png",
    "progress-objective-ratio-vs-worktime.png",
    "progress-objective-ratio-vs-tokens.png",
}


def test_write_progress_pngs_creates_overview_and_nine_details(tmp_path):
    outputs = write_progress_pngs(tmp_path, HISTORY, SCHEMA, CONTEXT)
    assert {path.name for path in outputs} == {"progress.png"} | DETAIL_OUTPUTS
    for path in outputs:
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
```

Add a legacy-data rendering test and change the current failure-preservation
test to fail one detail renderer, asserting that detail's previous bytes remain
while other outputs are present.

- [ ] **Step 6: Implement one generic panel renderer**

Use exactly:

```python
Y_KINDS = ("score", "objective", "ratio")
X_KINDS = ("round", "worktime", "tokens")
```

`_render_panel` selects fields from `Observation`, draws all-candidate scatter,
selected markers/score line, objective or ratio incumbent step line, and the
ratio `y=1.0` reference. It displays `No data` when no valid points exist.

Axis labels:

```text
Round
Cumulative worktime (hours)
Cumulative processed tokens
Score
<key>
<key> ratio (vs baseline)
```

Keep round tick thinning. Use Matplotlib's default numeric token formatter; do
not add custom K/M formatting.

- [ ] **Step 7: Render the overview and details from the same series**

`progress.png` uses `plt.subplots(3, 3, ...)` with y rows and x columns.
Each detail uses a single axis. Use one `_publish(output, render)` helper:

```python
temporary = output.with_name(f".{output.name}.tmp")
render(temporary)
os.replace(temporary, output)
```

Catch errors per file, preserve the previous file, clean the temporary
best-effort, and continue. Return only successfully published paths. Do not
implement cross-file transactions or HTML.

- [ ] **Step 8: Switch the loop refresh boundary after the API exists**

Change the helper to:

```python
def _refresh_progress_plot(store, plot_context=None):
    ...
    plot_mod.write_progress_pngs(
        store.run_dir,
        history,
        store.metrics_schema,
        plot_context or {},
    )
```

Every run path passes `telemetry.plot_context()`. `_record_failure` derives the
context from its optional tracker. Tests that call the helper directly may omit
context and retain legacy round-only behavior.

- [ ] **Step 9: Run all plot tests**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py -q
```

Expected: all tests pass and ten PNG files are validated.

- [ ] **Step 10: Commit**

```bash
git add simpleloop/plot.py simpleloop/tests/test_plot.py
git commit -m "feat: render multi-axis progress plots"
```

---

### Task 5: Remove Final Report And Verify The MVP

**Files:**
- Modify: `simpleloop/store.py`
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/views.py`
- Modify: `simpleloop/tests/test_views_and_parse.py`
- Modify: `simpleloop/tests/test_plot.py`
- Modify: `README.md`

**Interfaces:**
- Removes: `Store.write_final_report(goal: str) -> Path`
- Preserves: `_summary(...) -> dict`

- [ ] **Step 1: Change the existing no-op continue test to require no report**

Rename it to `test_noop_continue_refreshes_plots_without_report` and assert:

```python
assert (run_dir / "progress.png").exists()
assert not (run_dir / "final_report.md").exists()
```

Delete `test_final_report_uses_feedback`; keep
`test_store_persists_feedback`, because history remains the source of truth.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py \
  -k "without_report" -q
```

Expected: failure because continue mode still writes `final_report.md`.

- [ ] **Step 3: Remove report generation only**

Delete:

- `Store.write_final_report`;
- both `store.write_final_report(...)` calls in `run`;
- the final `report:` print;
- report-only comments in `views.py` and tests.

Keep best-commit recomputation because `_summary` and the completion log use it.
Do not delete old report files or historical specs.

- [ ] **Step 4: Update current README artifact documentation**

Replace:

```text
writes history.jsonl and final_report.md
```

with:

```text
writes history.jsonl, telemetry.json, progress.png, and nine detail progress images
```

Add `telemetry.py` and `plot.py` to the module table. Do not add a monitoring
configuration section because there is no monitoring configuration.

- [ ] **Step 5: Verify no live report references remain**

Run:

```bash
rg -n "write_final_report|final_report\\.md" simpleloop README.md
```

Expected: no output. Historical documents under `docs/superpowers` are
intentionally excluded.

- [ ] **Step 6: Run the full test suite**

Run:

```bash
python -m pytest simpleloop/tests/ -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 7: Commit**

```bash
git add simpleloop/store.py simpleloop/loop.py simpleloop/views.py \
  simpleloop/tests/test_views_and_parse.py simpleloop/tests/test_plot.py README.md
git commit -m "chore: remove final report generation"
```

---

## Final First-Principles And MVP Review

Before declaring the implementation complete, answer each item with evidence:

1. **User value:** Does every new persisted field directly support baseline,
   worktime, tokens, or one of the ten requested images? Delete fields that do
   not.
2. **Single source:** Are baseline and cumulative resources stored only in
   `telemetry.json`, with history containing point-in-time snapshots only?
   Remove duplicate ledgers.
3. **No speculative platform:** Confirm there is no monitoring config, formula
   parser, schema migration framework, per-call event log, pricing, dashboard,
   export API, or old-run backfill.
4. **Generic objective:** Search production code for `SPEED_MS` and
   `SPEED_UP`; neither may be hardcoded in telemetry or plotting.
5. **Exact arithmetic:** Confirm ratio is `current / baseline`, and processed
   tokens use only the four confirmed usage fields.
6. **Failure isolation:** Force one telemetry write failure and one PNG render
   failure; the run/history path must still succeed.
7. **Artifact scope:** Confirm new runs create one telemetry file and ten PNGs,
   and do not create `final_report.md`.
8. **Regression evidence:** Run the complete test suite fresh and report the
   exact pass/fail count.

Verification commands:

```bash
rg -n "SPEED_MS|SPEED_UP" simpleloop/telemetry.py simpleloop/plot.py
rg -n "price|cost|schema_version|migration|dashboard|export" \
  simpleloop/telemetry.py simpleloop/plot.py
rg -n "write_final_report|final_report\\.md" simpleloop README.md
python -m pytest simpleloop/tests/ -q
git diff --check
git status --short
```

Expected: the first three searches produce no output, tests report zero
failures, `git diff --check` is clean, and `git status --short` contains only
the intended implementation files before the final commit.
