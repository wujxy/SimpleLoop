# Open Researcher Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Proposer/Executor/Judger pipeline with an open Researcher/Executor loop whose Harness records staged gates and selects parents only from factual evaluation results.

**Architecture:** Keep the existing outer scheduler, worktree isolation, backends, and append-only run artifacts. Remove the Judger and semantic handoff fields; normalize path, eval-process, and configured metric gates into one factual candidate result that the next Researcher call can inspect. Preserve legacy run resumption by tolerating old manifests and history fields at read boundaries while writing only the new schema.

**Tech Stack:** Python 3.9+, dataclasses, JSON/YAML, Git worktrees, Apptainer, pytest, matplotlib.

## Global Constraints

- Work only on branch `refactor/open-researcher-loop`.
- Do not modify `.env`, credentials, production secrets, or evaluator/reference artifacts.
- The Goal, evaluator, declared Gates, editable artifact boundary, and resource budget remain authoritative.
- Do not add a planner, reviewer, critic, risk model, replacement score, semantic-memory agent, or Harness evolution.
- Preserve local and HEPJob execution, parallel candidates, resumption, static proposals, telemetry, export, and provenance.
- Use TDD: every behavior change begins with a focused failing test, followed by the smallest implementation that passes it.
- Commit each task independently with the commit message shown in that task.
- Known baseline failure remains out of scope: `tests/test_parallel_candidates.py::test_tiny_example_uses_parallel_objective_selection` expects three candidates while `examples/tiny_algo_opt/task.yaml` configures two.
- Baseline evidence before implementation: `353 passed, 1 failed` from `python -m pytest -q tests`.

---

## File Map

- `simpleloop/harness/gate.py`: changed-path checking plus normalized staged Gate records.
- `simpleloop/roles/executor.py`: execution result with explicit path-gate facts.
- `simpleloop/candidate_worker.py`: Executor -> path gate/commit -> eval -> factual result; no Judger.
- `simpleloop/harness/store.py`: new candidate/history schema, deterministic eligibility, legacy read compatibility.
- `simpleloop/harness/views.py`: compact factual Researcher projection.
- `simpleloop/harness/memory.py`: factual episode lookup; retire semantic Insights.
- `simpleloop/roles/proposer.py`: minimal exact-K proposal-string contract and open Researcher prompt.
- `simpleloop/loop.py`: factual round orchestration, deterministic selection, no reflection/insight/Judger state.
- `simpleloop/execution/{base,local,hepjob}.py`: proposal strings and reduced manifest/backend contracts.
- `simpleloop/reporting/plot.py`, `scripts/plot_details.py`: 2x3 objective/ratio reporting with no score.
- `simpleloop/prompts/*`, `simpleloop/self_improvement/*`: three-prompt self-improvement with legacy active-set migration.
- `README.md`, `pyproject.toml`, `simpleloop/__init__.py`, active example task files: public open-Researcher contract.
- `tests/test_gate_pipeline.py`: focused normalized Gate tests.
- Existing test modules: update integration, compatibility, reporting, prompt, backend, and configuration expectations.

---

### Task 1: Normalize Staged Gate Facts

**Files:**
- Create: `tests/test_gate_pipeline.py`
- Modify: `simpleloop/harness/gate.py`

**Interfaces:**
- Consumes: existing `gate.check_diff(changed_paths, editable, frozen)` and `metrics_schema` dictionaries.
- Produces: `gate.build_results(metrics_schema, *, paths, path_detail="", eval_commands=None, eval_detail="", metrics=None) -> dict[str, dict]` and `gate.all_passed(results) -> bool`.

- [ ] **Step 1: Write focused failing tests for the unified Gate view**

```python
from simpleloop.harness import gate


SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "FCN"}, {"key": "CONSISTENCY"}],
}


def test_build_results_records_harness_and_configured_gates():
    results = gate.build_results(
        SCHEMA,
        paths=True,
        eval_commands=False,
        eval_detail="exit codes: [7]",
        metrics={"FCN": True, "CONSISTENCY": False},
    )
    assert results == {
        "PATHS": {"passed": True, "detail": ""},
        "EVAL_COMMANDS": {"passed": False, "detail": "exit codes: [7]"},
        "FCN": {"passed": True, "detail": ""},
        "CONSISTENCY": {
            "passed": False,
            "detail": "evaluator reported FAIL",
        },
    }
    assert gate.all_passed(results) is False


def test_build_results_marks_short_circuited_gates_unknown():
    results = gate.build_results(
        SCHEMA,
        paths=False,
        path_detail="tests/x.py: touches a frozen path",
    )
    assert results["PATHS"]["passed"] is False
    assert results["EVAL_COMMANDS"] == {
        "passed": None,
        "detail": "not run because PATHS failed",
    }
    assert results["FCN"] == {
        "passed": None,
        "detail": "not run because PATHS failed",
    }


def test_build_results_treats_missing_metric_as_unknown():
    results = gate.build_results(
        SCHEMA, paths=True, eval_commands=True, metrics={"FCN": True}
    )
    assert results["CONSISTENCY"] == {
        "passed": None,
        "detail": "metric missing or unknown",
    }
    assert gate.all_passed(results) is False
```

- [ ] **Step 2: Run the Gate tests and verify the missing interface**

Run: `python -m pytest -q tests/test_gate_pipeline.py`

Expected: FAIL during collection because `simpleloop.harness.gate` has no `build_results`.

- [ ] **Step 3: Implement the normalized Gate helpers without changing path matching**

Add to `simpleloop/harness/gate.py`:

```python
PATHS = "PATHS"
EVAL_COMMANDS = "EVAL_COMMANDS"


def _result(passed: bool | None, detail: str = "") -> dict:
    return {"passed": passed, "detail": detail}


def build_results(
    metrics_schema: dict | None,
    *,
    paths: bool | None,
    path_detail: str = "",
    eval_commands: bool | None = None,
    eval_detail: str = "",
    metrics: dict | None = None,
) -> dict[str, dict]:
    results = {PATHS: _result(paths, path_detail)}
    configured = (metrics_schema or {}).get("gates", [])
    if paths is not True:
        reason = "not run because PATHS failed" if paths is False else "not run"
        results[EVAL_COMMANDS] = _result(None, reason)
        results.update({item["key"]: _result(None, reason) for item in configured})
        return results

    results[EVAL_COMMANDS] = _result(eval_commands, eval_detail)
    values = metrics or {}
    for item in configured:
        key = item["key"]
        value = values.get(key)
        detail = "" if value is True else (
            "evaluator reported FAIL" if value is False
            else "metric missing or unknown"
        )
        results[key] = _result(value if isinstance(value, bool) else None, detail)
    return results


def all_passed(results: dict[str, dict]) -> bool:
    return bool(results) and all(
        item.get("passed") is True for item in results.values()
    )
```

- [ ] **Step 4: Run focused and existing path-gate tests**

Run: `python -m pytest -q tests/test_gate_pipeline.py tests/test_views_and_parse.py -k 'gate_block or gate_to_bool or check_diff'`

Expected: PASS.

- [ ] **Step 5: Commit the Gate contract**

```bash
git add simpleloop/harness/gate.py tests/test_gate_pipeline.py
git commit -m "feat: normalize staged gate results"
```

---

### Task 2: Produce Factual Candidate Results Without a Judger

**Files:**
- Modify: `simpleloop/roles/executor.py`
- Modify: `simpleloop/candidate_worker.py`
- Modify: `tests/test_candidate_worker.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Consumes: Task 1 `gate.build_results` and `gate.all_passed`; existing `evals.run_eval` and `Workspace.commit`.
- Produces: `ExecResult.path_gate_passed`, `ExecResult.path_gate_violations`; reduced `CandidateSpec`; candidate dictionaries with `status`, `gates`, `gate_passed`, `eligible`, and no Judger fields.

- [ ] **Step 1: Replace Judger-based worker tests with factual terminal-state tests**

Update `tests/test_candidate_worker.py` fixtures so `CandidateSpec` is constructed with only `round_id`, `candidate_id`, `parent_sha`, `proposal`, and path/retry metadata. Build `CandidateDeps` without `judger_agent`.

Add these exact assertions to the worker tests:

```python
def test_run_candidate_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod.executor_mod, "execute", lambda *_a, **_k: ExecResult(
        sha="candidate", reason=None, changed_paths=["src/a.py"],
        path_gate_passed=True, path_gate_violations=[]))
    monkeypatch.setattr(worker_mod.evals, "run_eval", lambda *_a, **_k: EvalResult(
        "ok", {"SPEED_MS": 90.0, "CORRECTNESS": True}, (0,)))

    result = run_candidate(_deps(tmp_path), _spec(tmp_path))

    assert result["status"] == "COMPLETED"
    assert result["gate_passed"] is True
    assert result["eligible"] is True
    assert result["gates"]["EVAL_COMMANDS"]["passed"] is True
    assert not ({"score", "risk", "feedback", "feedback_for_proposer", "accepted"} & result.keys())


def test_path_gate_rejection_is_terminal_and_skips_eval(tmp_path, monkeypatch):
    called = False
    monkeypatch.setattr(worker_mod.executor_mod, "execute", lambda *_a, **_k: ExecResult(
        sha=None, reason="gate rejected", changed_paths=["tests/x.py"],
        path_gate_passed=False,
        path_gate_violations=["tests/x.py: touches a frozen path"]))
    def fake_eval(*_a, **_k):
        nonlocal called
        called = True
    monkeypatch.setattr(worker_mod.evals, "run_eval", fake_eval)

    result = run_candidate(_deps(tmp_path), _spec(tmp_path))

    assert called is False
    assert result["status"] == "PATH_GATE_REJECTED"
    assert result["sha"] is None
    assert result["gates"]["PATHS"]["passed"] is False
    assert result["gates"]["CORRECTNESS"]["passed"] is None


def test_nonzero_eval_command_is_a_gate_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod.executor_mod, "execute", lambda *_a, **_k: ExecResult(
        sha="candidate", reason=None, changed_paths=["src/a.py"],
        path_gate_passed=True, path_gate_violations=[]))
    monkeypatch.setattr(worker_mod.evals, "run_eval", lambda *_a, **_k: EvalResult(
        "failed", {"SPEED_MS": 90.0, "CORRECTNESS": True}, (7,)))

    result = run_candidate(_deps(tmp_path), _spec(tmp_path))

    assert result["status"] == "GATE_REJECTED"
    assert result["gates"]["EVAL_COMMANDS"]["passed"] is False
    assert result["gate_passed"] is False
    assert result["eligible"] is False
```

- [ ] **Step 2: Run worker tests and verify failures reference old Judger fields/contracts**

Run: `python -m pytest -q tests/test_candidate_worker.py`

Expected: FAIL because `ExecResult` lacks path-gate fields and the worker still requires a Judger.

- [ ] **Step 3: Extend `ExecResult` with explicit path-gate facts**

Use this dataclass in `simpleloop/roles/executor.py`:

```python
@dataclass
class ExecResult:
    sha: str | None
    reason: str | None
    changed_paths: list[str]
    path_gate_passed: bool
    path_gate_violations: list[str]
```

Return `path_gate_passed=True` for success and no-change, and `False` with the
exact violations for rejected paths. Keep the existing ordering: inspect
changed paths, check the path gate, then commit only a passing change.

- [ ] **Step 4: Reduce worker dependencies and manifest fields**

Change the public structures in `simpleloop/candidate_worker.py` to:

```python
@dataclass
class CandidateSpec:
    round_id: int
    candidate_id: int
    parent_sha: str
    proposal: str
    run_dir: str = ""
    worktree_path: str = ""
    result_dir: str = ""
    prompt_dir: str = ""
    attempt: int = 1


@dataclass
class CandidateDeps:
    cfg: dict
    run_dir: Path
    runtime: ApptainerRuntime
    workspace: Workspace
    executor_agent: Agent
    prompt_dir: Path | None = None
    gate_lines: str = ""
```

`CandidateSpec.from_dict` must access only these keys so legacy
`family`, `decision`, `prior_metrics`, and `baseline_metrics` are tolerated and
ignored.

- [ ] **Step 5: Replace `run_candidate` with the factual pipeline**

After Executor completion, build candidate records with this common shape:

```python
{
    "candidate": spec.candidate_id,
    "proposal": spec.proposal,
    "parent_sha": spec.parent_sha,
    "sha": result.sha,
    "status": status,
    "eval_block": eval_block,
    "metrics": eval_metrics,
    "changed_paths": result.changed_paths,
    "gates": gate_results,
    "gate_passed": gate_passed,
    "eligible": eligible,
    "selected": False,
}
```

Compute eligibility exactly as:

```python
objective_key = metrics_schema["objective"]["key"]
objective = eval_metrics.get(objective_key)
eligible = (
    result.sha is not None
    and gate_passed
    and isinstance(objective, (int, float))
    and not isinstance(objective, bool)
)
```

Catch Executor `AgentError`/`ValueError` as `EXECUTOR_FAILED`. Catch evaluation
exceptions after a commit as `EVAL_FAILED`, retain the SHA, set `PATHS=true`,
`EVAL_COMMANDS=false`, and configured Gates to `null`. Remove
`candidate_accepted`, `_status_for`, Judger imports/calls, and Judger-only
objective comparison context.

For `NO_CHANGE`, use `PATHS=true`, set `EVAL_COMMANDS` and configured Gates to
`null` with detail `not run because Executor produced no change`, and leave the
candidate in history with `eligible=false`. For a path rejection, join the
Executor's exact `path_gate_violations` into the `PATHS.detail` field.

- [ ] **Step 6: Make baseline worker output factual**

Keep baseline validation, but return:

```python
{
    "candidate": spec.candidate_id,
    "proposal": spec.proposal,
    "parent_sha": spec.parent_sha,
    "sha": spec.parent_sha,
    "status": "BASELINE",
    "eval_block": result.text,
    "metrics": result.metrics,
    "changed_paths": [],
    "gates": gate.build_results(
        cfg.get("metrics"), paths=True,
        eval_commands=result.commands_ok, metrics=result.metrics),
    "gate_passed": True,
    "eligible": True,
    "selected": False,
}
```

Update CLI catch-all results to the same schema and update `--baseline-only`
help text to say it skips the Executor.

- [ ] **Step 7: Run worker and runtime tests**

Run: `python -m pytest -q tests/test_candidate_worker.py tests/test_runtime.py`

Expected: PASS and no test imports `simpleloop.roles.judger`.

- [ ] **Step 8: Commit factual worker execution**

```bash
git add simpleloop/roles/executor.py simpleloop/candidate_worker.py tests/test_candidate_worker.py tests/test_runtime.py
git commit -m "refactor: return factual candidate results"
```

---

### Task 3: Persist Factual History and Select Deterministically

**Files:**
- Modify: `simpleloop/harness/store.py`
- Modify: `simpleloop/harness/views.py`
- Modify: `simpleloop/harness/memory.py`
- Modify: `simpleloop/harness/export.py`
- Modify: `tests/test_views_and_parse.py`
- Modify: `tests/test_memory.py`
- Modify: `tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: Task 2 candidate dictionaries.
- Produces: `store.eligible(candidate, metrics_schema)`, factual `Store.append_generation`, factual `views.for_proposer`, and legacy candidate normalization at read/selection boundaries.

- [ ] **Step 1: Replace history tests with the new persisted schema**

Add a generation test using:

```python
candidate = {
    "candidate": 0,
    "proposal": "replace the reconstruction kernel",
    "parent_sha": "parent",
    "sha": "candidate",
    "status": "COMPLETED",
    "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": True},
    "changed_paths": ["src/new_kernel.cc"],
    "gates": {
        "PATHS": {"passed": True, "detail": ""},
        "EVAL_COMMANDS": {"passed": True, "detail": ""},
        "CORRECTNESS": {"passed": True, "detail": ""},
    },
    "gate_passed": True,
    "eligible": True,
    "selected": False,
}
store.append_generation(
    0, parent_sha="parent", selected_candidate=0,
    selected_sha="candidate", candidates=[candidate])
row = store.history()[0]
assert row["candidates"][0]["status"] == "COMPLETED"
assert row["candidates"][0]["gates"]["PATHS"]["passed"] is True
assert not ({"score", "risk", "feedback", "feedback_for_proposer",
             "family", "decision", "accepted"} & row["candidates"][0].keys())
```

Add a legacy eligibility test:

```python
def test_legacy_high_risk_candidate_stays_ineligible():
    legacy = {
        "sha": "old", "risk": "high",
        "metrics": {"SPEED_MS": 80.0, "CORRECTNESS": True},
    }
    assert store_mod.eligible(legacy, SCHEMA) is False
```

- [ ] **Step 2: Run factual history and selector tests to verify old-field failures**

Run: `python -m pytest -q tests/test_views_and_parse.py tests/test_memory.py tests/test_parallel_candidates.py -k 'store or proposer_view or selector or memory'`

Expected: FAIL because Store still writes Judger fields and selector still uses risk/score.

- [ ] **Step 3: Rewrite `eligible` with explicit-new and legacy paths**

Use this decision order in `simpleloop/harness/store.py`:

```python
def eligible(candidate: dict, metrics_schema: dict) -> bool:
    if not candidate.get("sha"):
        return False
    objective = (candidate.get("metrics") or {}).get(
        metrics_schema["objective"]["key"])
    numeric = isinstance(objective, (int, float)) and not isinstance(objective, bool)
    if "eligible" in candidate:
        return candidate.get("eligible") is True and numeric
    if str(candidate.get("risk", "")).lower() == "high":
        return False
    metrics = candidate.get("metrics") or {}
    gates_pass = all(
        metrics.get(item["key"]) is True
        for item in metrics_schema.get("gates", [])
    )
    return gates_pass and numeric
```

This legacy branch is read compatibility only; new writes always contain
`eligible`.

- [ ] **Step 4: Simplify best-candidate tie-breaking and Store state**

Remove score from `best_candidate`, `Store.best_score`, and round summaries.
For equal objectives, keep the first candidate encountered; `_select_winner`
will explicitly sort candidate identifiers in Task 5. Change
`append_generation` to persist only the factual candidate keys from Task 2 and
round keys `round`, `parent_sha`, `selected_candidate`, `selected_sha`,
`base_sha`, `candidates`, and `telemetry` plus selected proposal/metrics/path
convenience fields.

- [ ] **Step 5: Make the Researcher view factual**

Return these candidate fields from `views.for_proposer`:

```python
{
    "candidate": candidate.get("candidate"),
    "proposal": candidate.get("proposal") or "",
    "parent_sha": candidate.get("parent_sha") or round_parent,
    "sha": candidate.get("sha"),
    "status": candidate.get("status") or candidate.get("candidate_status"),
    "selected": bool(candidate.get("selected")),
    "gate_passed": candidate.get("gate_passed", candidate.get("accepted")),
    "eligible": candidate.get("eligible"),
    "gates": candidate.get("gates") or {},
    "metrics": candidate.get("metrics") or {},
    "changed_paths": candidate.get("changed_paths") or [],
}
```

Do not project legacy `score`, `risk`, `feedback`, `feedback_for_proposer`,
`family`, or `decision`. Delete landing-state parsing.

- [ ] **Step 6: Retire semantic Insights and update episode lookup**

Remove `load_insights`, `render_insights`, `validate_insight`, and
`append_insight` from `memory.py` and their tests. Update `resolve_episode` to
return `ref`, `proposal`, `parent_sha`, `candidate_sha`, `status`, `selected`,
`gate_passed`, `eligible`, `gates`, `metrics`, and `changed_paths`.

- [ ] **Step 7: Update export language and run focused tests**

Change the no-best error in `harness/export.py` to
`no eligible best candidate (all gates pass + numeric objective)`.

Run: `python -m pytest -q tests/test_views_and_parse.py tests/test_memory.py tests/test_parallel_candidates.py tests/test_provenance_export_lock.py`

Expected: PASS for factual history, legacy selection, episode lookup, and export.

- [ ] **Step 8: Commit factual history and deterministic eligibility**

```bash
git add simpleloop/harness/store.py simpleloop/harness/views.py simpleloop/harness/memory.py simpleloop/harness/export.py tests/test_views_and_parse.py tests/test_memory.py tests/test_parallel_candidates.py
git commit -m "refactor: persist factual experiment history"
```

---

### Task 4: Open the Researcher Contract

**Files:**
- Modify: `simpleloop/roles/proposer.py`
- Modify: `simpleloop/prompts/proposer.md`
- Modify: `simpleloop/prompts/executor.md`
- Modify: `tests/test_prompt_templates.py`
- Modify: `tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: Task 3 `views.for_proposer`.
- Produces: `ProposalBatch.proposals: list[str]`, `_proposer_schema(K)`, `_parse_batch(data, candidates_per_round=K)`, and `propose(...) -> ProposalBatch` without Insights.

- [ ] **Step 1: Replace the Proposer schema/parser tests**

Use these expectations:

```python
def test_proposer_schema_is_exact_k_strings():
    schema = _proposer_schema(3)
    assert schema["required"] == ["proposals"]
    assert set(schema["properties"]) == {"proposals"}
    proposals = schema["properties"]["proposals"]
    assert proposals["minItems"] == proposals["maxItems"] == 3
    assert proposals["items"] == {
        "type": "string", "minLength": 1, "pattern": r"\S"
    }


def test_parse_batch_accepts_only_free_text_proposals():
    batch = _parse_batch(
        {"proposals": ["rewrite the algorithm", "replace its data model"]},
        candidates_per_round=2,
    )
    assert batch.proposals == [
        "rewrite the algorithm", "replace its data model"
    ]


@pytest.mark.parametrize("data", [
    {"reflection": "r", "proposals": ["p"]},
    {"proposals": []},
    {"proposals": [""]},
    {"proposals": ["   "]},
    {"proposals": [7]},
])
def test_parse_batch_rejects_nonminimal_contract(data):
    with pytest.raises(ValueError):
        _parse_batch(data, candidates_per_round=1)
```

- [ ] **Step 2: Run prompt/schema tests and verify old contract failures**

Run: `python -m pytest -q tests/test_prompt_templates.py tests/test_parallel_candidates.py -k 'proposer or executor or parse_batch'`

Expected: FAIL because the current schema requires reflection, insights,
family, and decision.

- [ ] **Step 3: Reduce the Proposer data model and parser**

Use:

```python
@dataclass
class ProposalBatch:
    proposals: list[str]
```

`_parse_batch` must require exactly `{"proposals"}`, require exactly K
nonblank strings, strip surrounding whitespace, and return the strings without
a local semantic length cap. Delete `Proposal`, reflection/insight constants,
family uniqueness checks, and `normalize_free_text` usage.
Delete the now-unused `normalize_free_text` helper and its tests instead of
leaving a second, hidden proposal policy in the role module.

- [ ] **Step 4: Render only factual Researcher context**

Remove the `insights` argument from `propose`. Render recent factual candidates
with proposal, parent SHA, candidate SHA, status, selected, gate results,
metrics, and changed paths. Keep raw eval output out of the default prompt.

The fixed prompt ending must be exactly equivalent to:

```text
Available evidence:
- The accepted revision and factual experiment index are starting points.
- Historical candidate SHAs and persisted run artifacts are available for read-only investigation.

Fixed boundaries:
- Generate exactly {candidates_per_round} executable experiment instructions.
- Every candidate starts from the accepted revision above.
- Editable paths: {editable}
- Frozen paths: {frozen}
- Return one JSON object containing only proposals, an array of nonblank strings.
```

Do not prescribe `git diff`, `git show`, a checklist, reflection, search family,
continue/switch, proposal granularity, or implementation scale.

- [ ] **Step 5: Replace semantic prompts with open role identities**

Set `simpleloop/prompts/proposer.md` to:

```markdown
You are the RESEARCHER responsible for achieving the user's Goal.

You investigate the current artifact and experimental evidence, decide what to
try next, and give each selected experiment to the Executor. The current
implementation and history are evidence and starting points, not constraints on
the form, scale, or algorithmic character of a solution.

Any implementation strategy is admissible within the editable artifact when
the Harness Gates pass. You decide how to investigate, what conclusions the
evidence supports, and how much implementation detail an experiment needs.
```

Set `simpleloop/prompts/executor.md` to:

```markdown
You are the EXECUTOR working directly for the Researcher.

You turn one Proposal into the strongest complete implementation you can within
the assigned worktree and resource budget. You own implementation investigation,
design, editing, and local verification. The Proposal may call for a small
change, a broad refactor, a replacement algorithm, or new production code.

The Harness owns commits, evaluation, Gates, and artifact selection.
```

- [ ] **Step 6: Run all prompt and proposer tests**

Run: `python -m pytest -q tests/test_prompt_templates.py tests/test_parallel_candidates.py -k 'proposer or executor or parse_batch or schema'`

Expected: PASS, with assertions that the rendered prompt lacks
`reflection`, `insight_refs`, `continue`, `switch`, `family`, and Judger fields.

- [ ] **Step 7: Commit the open Researcher contract**

```bash
git add simpleloop/roles/proposer.py simpleloop/prompts/proposer.md simpleloop/prompts/executor.md tests/test_prompt_templates.py tests/test_parallel_candidates.py
git commit -m "refactor: open the researcher contract"
```

---

### Task 5: Simplify the Outer Loop and Both Execution Backends

**Files:**
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/execution/base.py`
- Modify: `simpleloop/execution/local.py`
- Modify: `simpleloop/execution/hepjob.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_hepjob_backend.py`
- Modify: `tests/test_config_execution.py`

**Interfaces:**
- Consumes: Task 2 `CandidateSpec`/candidate results, Task 3 `store.eligible`, Task 4 proposal strings.
- Produces: backend `run_candidates(*, proposals: list[str], round_id: int, parent_sha: str, journal=None) -> list[dict]`; no Judger agents or prior/baseline metric manifest context.

- [ ] **Step 1: Update integration fakes to proposal strings and factual candidates**

Use proposal batches such as `['rewrite kernel', 'replace data layout']` rather
than dictionaries. Fake candidates must return Task 2 fields. Add an assertion
that `RunContext` has no `judger_agent` and that `_build_context` constructs
exactly two agents with tool sets `Read,Bash` and `Read,Edit,Write,Bash`.

- [ ] **Step 2: Add deterministic selector tests without score/risk**

```python
def test_selector_uses_only_eligibility_objective_and_candidate_order():
    candidates = [
        {"candidate": 1, "sha": "b", "eligible": True,
         "metrics": {"SPEED_MS": 80.0}},
        {"candidate": 0, "sha": "a", "eligible": True,
         "metrics": {"SPEED_MS": 80.0}},
        {"candidate": 2, "sha": "faster-but-rejected", "eligible": False,
         "metrics": {"SPEED_MS": 70.0}},
    ]
    assert _select_winner(
        candidates, SCHEMA, prior_metrics={"SPEED_MS": 100.0}
    )["sha"] == "a"
```

- [ ] **Step 3: Run loop/backend tests and verify signature failures**

Run: `python -m pytest -q tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_config_execution.py`

Expected: FAIL because backends still expect proposal dictionaries,
prior/baseline metrics, and Judger dependencies.

- [ ] **Step 4: Remove Judger and semantic state from `RunContext` and round journals**

Delete `judger_agent`, `insights_path`, Judger construction, pending Insights,
reflection, and semantic fields from in-flight metadata. `_next_proposals`
returns `list[str]`; static proposal mode returns the same type. A normal round
passes proposal strings directly to the backend and writes only factual
candidate results.

- [ ] **Step 5: Reduce backend interfaces**

Set the abstract and concrete method signature to:

```python
def run_candidates(
    self,
    *,
    proposals: list[str],
    round_id: int,
    parent_sha: str,
    journal=None,
) -> list[dict]:
    ...
```

Remove `prior_metrics` and `baseline_metrics` from candidate dispatch and
manifests. Baseline metrics remain in `RunContext` for telemetry, summary, and
incumbent comparison only.

- [ ] **Step 6: Update local candidate construction and failures**

Construct:

```python
spec = candidate_worker.CandidateSpec(
    round_id=round_id,
    candidate_id=candidate_id,
    parent_sha=parent_sha,
    proposal=proposal,
    run_dir=str(ctx.run_dir),
    worktree_path=str(worktree),
    prompt_dir=str(ctx.prompt_dir or ""),
)
```

Pass proposal strings to `_candidate_failure`; populate the factual schema with
`status="WORKER_FAILED"`, null Gates, and no semantic fields.

- [ ] **Step 7: Update HEPJob manifests and collection**

Write new manifests from proposal strings and the reduced `CandidateSpec`.
Validate collected business results by requiring a string `status` rather than
`candidate_status`. Preserve the atomic `result.json` + `usage.json` +
`_FINISHED` contract and infrastructure retry state machine.

Legacy manifest JSON containing extra family/decision/metric fields must parse.
Legacy completed `result.json` containing `candidate_status` must be normalized
on collection to `status` so an in-flight pre-refactor job can finish.

- [ ] **Step 8: Make winner selection deterministic and factual**

Use:

```python
winner = min(
    eligible_candidates,
    key=lambda candidate: (
        direction * candidate["metrics"][objective_key],
        int(candidate.get("candidate") or 0),
    ),
)
```

Retain the incumbent-improvement check. Remove `_candidate_accepted`, score
tie-breaking, best-score printing, and `best_score` from `_summary`.

- [ ] **Step 9: Run loop, backend, resume, and worker tests**

Run: `python -m pytest -q tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_candidate_worker.py tests/test_config_execution.py tests/test_provenance_export_lock.py`

Expected: PASS except the documented tiny-example mismatch if the whole
`test_parallel_candidates.py` module includes it; verify all other tests pass
with `-k 'not test_tiny_example_uses_parallel_objective_selection'`.

- [ ] **Step 10: Commit orchestration/backend simplification**

```bash
git add simpleloop/loop.py simpleloop/execution/base.py simpleloop/execution/local.py simpleloop/execution/hepjob.py tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_config_execution.py
git commit -m "refactor: simplify researcher experiment orchestration"
```

---

### Task 6: Remove Score Reporting

**Files:**
- Modify: `simpleloop/reporting/plot.py`
- Modify: `scripts/plot_details.py`
- Modify: `tests/test_plot.py`
- Modify: `tests/test_telemetry.py`

**Interfaces:**
- Consumes: Task 3 factual history and existing telemetry context.
- Produces: 2x3 `progress.png` and six offline detail panels for objective/ratio against round/worktime/tokens.

- [ ] **Step 1: Rewrite plot tests for objective/ratio-only observations**

Assert:

```python
assert plot_mod._Y_KINDS == ("objective", "ratio")
series = plot_mod.build_series(history, schema, context)
assert series.objective_points == [(1, 90.0)]
assert series.selected_objectives == [(1, 90.0)]
assert not hasattr(series, "score_points")
```

Update PNG tests to expect six detail names and a `2 x 3` overview axis array.
Legacy history fixtures may retain score keys to prove they are ignored.

- [ ] **Step 2: Run plotting tests and verify score-dimension failures**

Run: `python -m pytest -q tests/test_plot.py tests/test_telemetry.py`

Expected: FAIL because `Observation`, `_Y_KINDS`, and rendering still include score.

- [ ] **Step 3: Remove score from plot data and rendering**

Delete `Observation.score`, `score_points`, `selected_scores`, the `score`
parameter to `_observation`, score labels/styles, and score extraction from
history. Set:

```python
_Y_KINDS = ("objective", "ratio")
```

Create overview axes with:

```python
figure, axes = plt.subplots(
    len(_Y_KINDS), len(_X_KINDS), figsize=(16, 9), squeeze=False
)
```

Keep incumbent objective/ratio traces and baseline ratio line unchanged.

- [ ] **Step 4: Update offline detail generation and run plotting tests**

Ensure `scripts/plot_details.py` iterates the shared two-value `_Y_KINDS` and
produces exactly six files.

Run: `python -m pytest -q tests/test_plot.py tests/test_telemetry.py`

Expected: PASS.

- [ ] **Step 5: Commit report simplification**

```bash
git add simpleloop/reporting/plot.py scripts/plot_details.py tests/test_plot.py tests/test_telemetry.py
git commit -m "refactor: remove judger score reporting"
```

---

### Task 7: Migrate Prompt Self-Improvement to Three Prompts

**Files:**
- Modify: `simpleloop/prompts/__init__.py`
- Modify: `simpleloop/prompts/meta_optimizer.md`
- Modify: `simpleloop/self_improvement/history.py`
- Modify: `simpleloop/self_improvement/optimizer.py`
- Modify: `simpleloop/self_improvement/gate.py`
- Modify: `tests/test_prompt_self_improvement.py`

**Interfaces:**
- Consumes: new `proposer.md`, `executor.md`, and existing prompt history state.
- Produces: `PROMPT_NAMES = ("proposer", "executor", "meta_optimizer")` and active-set migration that removes `judger.md` while preserving historical snapshots.

- [ ] **Step 1: Add prompt-set and legacy-lineage migration tests**

```python
def test_prompt_names_exclude_judger():
    assert PROMPT_NAMES == ("proposer", "executor", "meta_optimizer")


def test_existing_lineage_migrates_active_prompt_set(tmp_path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.prompt_dir.mkdir(parents=True)
    snapshot = history.history_dir / "v000"
    snapshot.mkdir(parents=True)
    legacy_meta = load_semantic("meta_optimizer").replace(
        "Researcher, Executor, and Harness",
        "Proposer, Executor, Judger, and Harness",
        1,
    )
    for name, value in {
        "proposer": load_semantic("proposer"),
        "executor": load_semantic("executor"),
        "judger": "legacy judger",
        "meta_optimizer": legacy_meta,
    }.items():
        (history.prompt_dir / f"{name}.md").write_text(value, encoding="utf-8")
        (snapshot / f"{name}.md").write_text(value, encoding="utf-8")
    (snapshot / "manifest.yaml").write_text("version: v000\n", encoding="utf-8")
    history._write_state({"active_version": "v000", "inflight": None})

    assert history.initialize() == "v000"
    assert not (history.prompt_dir / "judger.md").exists()
    assert (snapshot / "judger.md").read_text(encoding="utf-8") == "legacy judger"
    assert identity_core(
        (history.prompt_dir / "meta_optimizer.md").read_text(encoding="utf-8")
    ) == identity_core(load_semantic("meta_optimizer"))
    assert PromptGate().check(history.prompt_dir) == []
```

Import `identity_core` in the test. The fixture deliberately contains all four
legacy active files and a state pointing at `v000`; migration must update only
the active set and preserve the historical snapshot byte-for-byte.

- [ ] **Step 2: Run self-improvement tests and verify four-prompt failures**

Run: `python -m pytest -q tests/test_prompt_self_improvement.py`

Expected: FAIL because `PROMPT_NAMES` and optimizer write access still include Judger.

- [ ] **Step 3: Reduce package and optimizer prompt sets**

Set:

```python
PROMPT_NAMES = ("proposer", "executor", "meta_optimizer")
```

Remove Judger from `MetaOptimizer.run` write-access text. Rewrite the immutable
Meta Optimizer identity core around Researcher, Executor, and Harness facts;
retain the same core markers and prompt-only responsibility. Include the exact
phrase `Researcher, Executor, and Harness` inside the new core so the legacy
fixture above creates a different, valid marked core.

- [ ] **Step 4: Normalize existing active prompt sets**

In `PromptHistory.initialize`, when `active_version` already exists, call a new
`_normalize_active_set()` before returning. The helper must:

```python
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
```

Call the same helper after `restore()` so recovering an old snapshot cannot
reintroduce Judger or the old identity core. Do not modify files inside
historical version directories.

- [ ] **Step 5: Verify static Gate rejection after migration**

After initialization migration, explicitly recreate `judger.md` in the active
prompt directory and assert `PromptGate.check` reports it as unexpected. Keep
identity-core mutation, symlink, length, rollback, and snapshot tests passing.

Run: `python -m pytest -q tests/test_prompt_self_improvement.py`

Expected: PASS.

- [ ] **Step 6: Commit three-prompt self-improvement**

```bash
git add simpleloop/prompts/__init__.py simpleloop/prompts/meta_optimizer.md simpleloop/self_improvement/history.py simpleloop/self_improvement/optimizer.py simpleloop/self_improvement/gate.py tests/test_prompt_self_improvement.py
git commit -m "refactor: migrate self improvement off judger"
```

---

### Task 8: Remove Judger Artifacts, Open Default Tasks, and Update Documentation

**Files:**
- Delete: `simpleloop/roles/judger.py`
- Delete: `simpleloop/prompts/judger.md`
- Modify: `simpleloop/roles/__init__.py`
- Modify: `simpleloop/cli.py`
- Modify: `simpleloop/config.py`
- Modify: `simpleloop/harness/__init__.py`
- Modify: `simpleloop/harness/evals.py`
- Modify: `simpleloop/harness/workspace.py`
- Modify: `simpleloop/__init__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/simpleloop_prompt_self_improvement_mvp.md`
- Modify: `examples/omilrec-opt/README.md`
- Modify: `examples/omilrec-post-v107-opt/README.md`
- Modify: `examples/tiny_algo_opt/README.md`
- Modify: `examples/task.yaml`
- Modify: `examples/omilrec-opt/task.yaml`
- Modify: `examples/omilrec-v100-opt/task.yaml`
- Modify: `examples/omilrec-post-v107-opt/task.yaml`
- Modify: `examples/tiny_algo_opt/task.yaml` only to remove obsolete Judger comments; do not change candidate count.
- Modify: `tests/test_agent_usage.py`
- Modify: `tests/test_prompt_templates.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_prompt_self_improvement.py`
- Modify: `tests/test_config_execution.py`

**Interfaces:**
- Consumes: all preceding tasks.
- Produces: public two-agent architecture and non-prescriptive default task examples.

- [ ] **Step 1: Add public-text/config assertions before deleting artifacts**

Extend example tests to assert default OMILREC tasks:

```python
goal = raw["task"]["goal"].lower()
assert "speed_ms" in goal
assert "every configured gate" in goal
for anchored in ("hoisting", "caching", "soa", "safe", "forbidden",
                 "second likelihood"):
    assert anchored not in goal
assert "OMILRECV2/src/**" in raw["safety"]["editable_paths"]
assert "OMILRECV2/CMakeLists.txt" in raw["safety"]["editable_paths"]
```

Add repository assertions that no runtime Python module imports
`simpleloop.roles.judger` and no packaged prompt name is `judger`.

- [ ] **Step 2: Run public-text tests and verify old architecture failures**

Run: `python -m pytest -q tests/test_prompt_templates.py tests/test_parallel_candidates.py tests/test_prompt_self_improvement.py -k 'example or prompt or judger or architecture'`

Expected: FAIL because files and docs still expose the Judger architecture.

- [ ] **Step 3: Delete Judger code and update module/package descriptions**

Delete the two Judger files. Update docstrings and `pyproject.toml` description
to `Researcher -> Executor -> Harness evaluation`. Remove obsolete Judger
wording from CLI help/output, config comments/errors, workspace/eval docstrings,
roles, Harness package docs, runtime logs, examples, and tests. CLI completion
prints the best SHA, objective, and round count without a score.

- [ ] **Step 4: Open default task Goals and editable artifacts**

Use this outcome-only Goal in active OMILREC default tasks, with the concrete
metric/gate names adjusted to each file:

```yaml
task:
  goal: >
    Minimize reconstruction SPEED_MS while satisfying every configured gate.
    The current implementation is the starting artifact, not a constraint on
    the form or scale of a valid solution.
```

Use:

```yaml
safety:
  editable_paths:
    - "OMILRECV2/src/**"
    - "OMILRECV2/CMakeLists.txt"
```

Remove `OMILRECV2/CMakeLists.txt` from frozen paths. Keep evaluator scripts,
tests, references, benchmarks, thresholds, root build/measurement wiring, and
documentation frozen. Reduce Gate descriptions to observable checks and exact
thresholds; remove technique advice and SAFE/FORBIDDEN predictions.

Keep explicitly named `task_hints.yaml`/`task_nohints.yaml` controlled fixtures
unchanged except where stale Judger terminology would break validation.

- [ ] **Step 5: Rewrite README and current self-improvement documentation**

Document:

```text
Goal -> Researcher -> Executor -> path gate/commit -> Harness eval/gates
     -> factual SHA/metrics/history -> next Researcher call
```

Explain staged Gates, all-attempt history, `gate_passed`/`eligible`/`selected`,
open solution scale, objective-only selection, 2x3 reporting, and three-prompt
self-improvement. Do not edit historical dated specs/plans/reports merely to
erase accurate history.

- [ ] **Step 6: Run a repository-wide stale-runtime-reference check**

Run:

```bash
rg -n "roles\.judger|judger_agent|Judgment|feedback_for_proposer|LANDED_STATE|best_score" simpleloop tests README.md pyproject.toml scripts
```

Expected: no runtime references. Legacy compatibility fixtures may contain
literal old history keys only when their test name states that purpose.

- [ ] **Step 7: Run focused public-contract tests**

Run: `python -m pytest -q tests/test_prompt_templates.py tests/test_parallel_candidates.py tests/test_prompt_self_improvement.py tests/test_config_execution.py`

Expected: all pass except the documented tiny-example candidate-count mismatch.

- [ ] **Step 8: Commit public architecture cleanup**

```bash
git add simpleloop README.md pyproject.toml docs/simpleloop_prompt_self_improvement_mvp.md \
  examples/task.yaml examples/omilrec-opt examples/omilrec-post-v107-opt \
  examples/tiny_algo_opt tests/test_agent_usage.py tests/test_prompt_templates.py \
  tests/test_parallel_candidates.py tests/test_prompt_self_improvement.py \
  tests/test_config_execution.py
git commit -m "docs: publish the open researcher loop"
```

---

### Task 9: Final Verification and Compatibility Audit

**Files:**
- Modify only files implicated by failures found in this task.

**Interfaces:**
- Consumes: completed Tasks 1-8.
- Produces: verified branch with no regressions beyond the documented baseline mismatch.

- [ ] **Step 1: Compile all Python modules**

Run: `python -m compileall -q simpleloop scripts`

Expected: exit 0 with no output.

- [ ] **Step 2: Run the full core suite excluding the single known baseline mismatch**

Run: `python -m pytest -q tests -k 'not test_tiny_example_uses_parallel_objective_selection'`

Expected: all selected tests pass; one test is deselected.

- [ ] **Step 3: Reconfirm the known failure is unchanged**

Run: `python -m pytest -q tests/test_parallel_candidates.py::test_tiny_example_uses_parallel_objective_selection`

Expected: the same pre-existing assertion `assert 2 == 3`; no different error.

- [ ] **Step 4: Run the repository-wide stale-field audit**

Run:

```bash
rg -n "judger_agent|roles\.judger|prompts/judger|feedback_for_proposer|LANDED_STATE|best_score" simpleloop README.md pyproject.toml scripts
```

Expected: no matches.

Run:

```bash
rg -n '"(score|risk|feedback|family|decision|reflection|accepted)"' simpleloop
```

Expected: matches only in explicitly documented legacy read-normalization code;
new result construction and new history writes contain none.

- [ ] **Step 5: Verify Git diff hygiene**

Run: `git diff --check d5416ae..HEAD && git status --short`

Expected: no whitespace errors and a clean worktree.

- [ ] **Step 6: Commit any verification-only corrections**

If Steps 1-5 required a correction, stage only the implicated files and commit:

```bash
git add -u
git commit -m "fix: close open researcher verification gaps"
```

If no correction was required, do not create an empty commit.
