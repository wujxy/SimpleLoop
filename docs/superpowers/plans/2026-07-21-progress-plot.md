# Per-Round Progress Plot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redraw `run_dir/progress.png` after every completed SimpleLoop round with candidate score/objective scatter points and selected/incumbent lines.

**Architecture:** A new `simpleloop/plot.py` transforms persisted serial or parallel history into plot series, then renders a headless Matplotlib PNG through an atomic temporary-file replacement. The loop invokes the renderer only after a complete history record is persisted, and final reports reference the resulting image.

**Tech Stack:** Python 3.9+, Matplotlib `Agg`, pytest, JSONL history records.

## Global Constraints

- The plotting module must be named `simpleloop/plot.py`.
- Generate only one PNG artifact: `run_dir/progress.png`.
- Plot failures must not terminate or alter the optimization lineage.
- `history.jsonl` remains the only chart data source.
- Candidate indexes must not be connected across rounds.
- Missing metrics are omitted rather than estimated.

---

### Task 1: History-To-Series Transformation

**Files:**
- Create: `simpleloop/plot.py`
- Create: `simpleloop/tests/test_plot.py`

**Interfaces:**
- Consumes: `history: list[dict]`, `metrics_schema: dict | None`
- Produces: `build_series(history, metrics_schema) -> PlotSeries`
- Produces: `PlotSeries` fields for candidate scores, selected scores, candidate objectives, incumbent objectives, objective key, and direction.

- [ ] **Step 1: Write failing parallel and serial transformation tests**

```python
def test_build_series_tracks_candidates_selected_and_incumbent():
    series = build_series(history, schema)
    assert series.score_points == [(1, 0.4), (1, 0.8), (2, 0.3)]
    assert series.selected_scores == [(1, 0.8)]
    assert series.objective_points == [(1, 120.0), (1, 90.0), (2, 110.0)]
    assert series.incumbent_objective == [(1, 90.0), (2, 90.0)]
```

Include a serial record test where `accepted=True` establishes the incumbent,
and include missing/non-numeric metric values that must be skipped.

- [ ] **Step 2: Run transformation tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py -k build_series -q
```

Expected: collection/import failure because `simpleloop.plot` does not exist.

- [ ] **Step 3: Implement the pure transformation**

Define:

```python
@dataclass
class PlotSeries:
    rounds: list[int]
    score_points: list[tuple[int, float]]
    selected_scores: list[tuple[int, float]]
    objective_points: list[tuple[int, float]]
    selected_objectives: list[tuple[int, float]]
    incumbent_objective: list[tuple[int, float]]
    objective_key: str | None
    lower_is_better: bool | None


def build_series(history: list[dict], metrics_schema: dict | None) -> PlotSeries:
    objective = (metrics_schema or {}).get("objective") or {}
    objective_key = objective.get("key")
    lower_is_better = objective.get("lower_is_better")
    rounds = []
    score_points = []
    selected_scores = []
    objective_points = []
    selected_objectives = []
    incumbent_objective = []
    incumbent = None

    for index, record in enumerate(history):
        round_number = int(record.get("round", index)) + 1
        rounds.append(round_number)
        parallel = isinstance(record.get("candidates"), list)
        candidates = record["candidates"] if parallel else [record]
        selected_id = record.get("selected_candidate") if parallel else None
        for candidate in candidates:
            selected = (
                candidate.get("candidate") == selected_id
                if parallel
                else bool(record.get("accepted"))
            )
            score = candidate.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                point = (round_number, float(score))
                score_points.append(point)
                if selected:
                    selected_scores.append(point)
            value = (candidate.get("metrics") or {}).get(objective_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                point = (round_number, float(value))
                objective_points.append(point)
                if selected:
                    selected_objectives.append(point)
                    incumbent = float(value)
        if incumbent is not None:
            incumbent_objective.append((round_number, incumbent))

    return PlotSeries(
        rounds, score_points, selected_scores, objective_points,
        selected_objectives, incumbent_objective, objective_key,
        lower_is_better,
    )
```

Use one-based round numbers. For a parallel generation, derive selected state
from `selected_candidate`; for a serial record, derive it from `accepted`.
Update the incumbent only from a selected/accepted record with a numeric
objective. Carry an established incumbent through later no-winner rounds.

- [ ] **Step 4: Run transformation tests and verify GREEN**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py -k build_series -q
```

Expected: all selected tests pass.

---

### Task 2: Headless Atomic PNG Rendering

**Files:**
- Modify: `pyproject.toml`
- Modify: `simpleloop/plot.py`
- Modify: `simpleloop/tests/test_plot.py`

**Interfaces:**
- Consumes: `run_dir`, complete history, metrics schema.
- Produces: `write_progress_png(run_dir, history, metrics_schema) -> Path | None`.
- Internal boundary: `_render_progress_png(series, output_path) -> None`.

- [ ] **Step 1: Add failing rendering and failure-preservation tests**

```python
def test_write_progress_png_creates_png(tmp_path):
    output = write_progress_png(tmp_path, history, schema)
    assert output == tmp_path / "progress.png"
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_render_failure_preserves_previous_png(monkeypatch, tmp_path):
    output = tmp_path / "progress.png"
    output.write_bytes(b"previous")
    monkeypatch.setattr(plot_mod, "_render_progress_png",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert write_progress_png(tmp_path, history, schema) is None
    assert output.read_bytes() == b"previous"
```

- [ ] **Step 2: Run rendering tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py -k 'progress_png or render_failure' -q
```

Expected: failure because the rendering API is not implemented.

- [ ] **Step 3: Add Matplotlib and implement the renderer**

Add `matplotlib>=3.5` to project dependencies. In `simpleloop/plot.py`, select
`Agg` before importing `pyplot`, render a fixed-size two-row figure, and use:

```python
temporary = run_dir / ".progress.tmp.png"
_render_progress_png(series, temporary)
os.replace(temporary, output)
```

Score uses candidate scatter points plus the selected score line with y-limits
`0..1`. Objective uses candidate scatter points plus selected markers and an
incumbent step line. Titles include the objective key and direction. Rendering
exceptions print a `[plot] warning:` prefix followed by the exception string,
clean the temporary file best-effort, return `None`, and preserve the previous
output.

- [ ] **Step 4: Run plotting tests and verify GREEN**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py -q
```

Expected: all plot tests pass.

- [ ] **Step 5: Commit the transformation and renderer**

```bash
git add pyproject.toml simpleloop/plot.py simpleloop/tests/test_plot.py
git commit -m "feat: render per-round progress plot"
```

---

### Task 3: Per-Round Loop Integration And Report Link

**Files:**
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/store.py`
- Modify: `simpleloop/tests/test_plot.py`
- Modify: `simpleloop/tests/test_views_and_parse.py`

**Interfaces:**
- Consumes: `Store.history()`, `Store.run_dir`, `Store.metrics_schema`.
- Produces: `_refresh_progress_plot(store: Store) -> None`.

- [ ] **Step 1: Add failing update-hook and report tests**

Test that `_refresh_progress_plot` forwards persisted history and schema to
`plot.write_progress_png`. Extend the final-report test with:

```python
assert "![Run progress](progress.png)" in report_text
```

- [ ] **Step 2: Run integration tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py simpleloop/tests/test_views_and_parse.py -k 'refresh_progress or final_report' -q
```

Expected: failure because the hook and Markdown image do not exist.

- [ ] **Step 3: Integrate after every persisted round**

Import `simpleloop.plot` in `loop.py` and define:

```python
def _refresh_progress_plot(store: Store) -> None:
    plot_mod.write_progress_png(
        store.run_dir,
        store.history(),
        store.metrics_schema,
    )
```

Call it immediately after:

- `store.append_generation` in automatic self-loop mode.
- `store.append` in the successful serial/static path.
- `store.append` inside `_record_failure`.

Add `![Run progress](progress.png)` near the top of `final_report.md`.

- [ ] **Step 4: Run integration tests and verify GREEN**

Run:

```bash
python -m pytest simpleloop/tests/test_plot.py simpleloop/tests/test_views_and_parse.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit loop and report integration**

```bash
git add simpleloop/loop.py simpleloop/store.py simpleloop/tests/test_plot.py simpleloop/tests/test_views_and_parse.py
git commit -m "feat: refresh progress plot after each round"
```

---

### Task 4: Full Verification And Real-History Smoke Test

**Files:**
- Modify only if verification exposes a defect.

**Interfaces:**
- Uses: the completed plotting API and an existing parallel run history.
- Produces: a verified `progress.png`.

- [ ] **Step 1: Run the complete project test suite**

Run:

```bash
python -m pytest simpleloop/tests -q
python -m compileall -q simpleloop
git diff --check
```

Expected: all tests pass, compilation succeeds, and no whitespace errors exist.

- [ ] **Step 2: Render an existing parallel history**

Run a short Python command that loads
`runs/tiny-algo-parallel-002/history.jsonl`, calls `write_progress_png`, and
writes `runs/tiny-algo-parallel-002/progress.png`.

Expected: a non-empty PNG beginning with the PNG signature.

- [ ] **Step 3: Inspect the generated image**

Use the local image viewer to verify:

- Both panels are nonblank.
- Candidate scatter points and selected/incumbent lines are distinguishable.
- Round labels are one-based and readable.
- No title, legend, or tick labels overlap.

- [ ] **Step 4: Report verification evidence**

Report the exact test count, generated image path, and any verification that
could not be performed.
