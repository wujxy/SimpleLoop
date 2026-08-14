# SimpleLoop Phase 0–1 Typed Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze the RSI-enabled behavior, then replace proposer/candidate protocol dictionaries inside the running Kernel with small frozen Request/Result contracts and explicit codecs.

**Architecture:** Phase 0 repairs the known test baseline and characterizes five durable formats. Phase 1 keeps the current loop, Local/HEPJob backends, Apptainer runtime, worker layout, and RSI implementation, but makes proposer collection return `ProposalBatch`, candidate execution return `CandidateResult`, selection return `Selection`, and history persistence consume `RoundResult`. JSON dictionaries remain only at worker, resume, legacy-history, and reporting boundaries.

**Tech Stack:** Python 3.9+, frozen dataclasses, enums, JSON/JSONL, pytest, Git worktrees, Apptainer, Local and HTCondor adapters.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-14-simpleloop-typed-pipeline-refactor-design.md`.
- Do not change optimization semantics: abstention, candidate statuses, hard gates, objective selection, incumbent advancement, retry/resume, or RSI KEEP/CHANGE/adoption.
- Keep `proposer/` independent. After Phase 1, `simpleloop/loop.py` and `simpleloop/execution/proposer_lanes.py` import no `proposer.*` module.
- Keep `CandidateSpec` as the worker manifest in this phase. Candidate pipeline extraction is Phase 2; World/Sandbox extraction is Phase 3.
- Add only contracts used by a live boundary. Do not add unused Loop, World, Scheduler, or replacement RSI models.
- `CandidateResult` is the sole in-memory candidate business result. Worker/history dictionaries are projections, not parallel models.
- Raw proposer worker JSON may retain `research_target` and `material_difference`; Host `Proposal` exposes only `instruction` and `evidence_refs`.
- Old inflight journals remain readable. New journals keep the same top-level schema but store only canonical Host proposal fields.
- Do not edit `.env`, credentials, production configuration, or the untracked `.superpowers/` directory.
- Use TDD and make one focused commit per task.

---

## Completion Boundaries

Phase 0 is complete when:

- `python -m pytest -q` collects only `tests/` and passes;
- commit `ebe3661` is an ancestor of `HEAD`;
- candidate worker, proposer worker, history, resume, and RSI formats have golden fixtures;
- production code is unchanged.

Phase 1 is complete when:

- proposer collection returns `ProposalBatch`;
- Local/HEPJob candidate paths return `CandidateResult`;
- selection returns `Selection` without mutating candidates;
- `Store` writes one `RoundResult` projection;
- current-round business functions do not index candidate dictionaries;
- old worker/history/resume inputs remain readable;
- the full suite passes from the repository root.

---

### Task 1: Restore a clean repository-root test baseline

**Necessity:** Current failures are known baseline defects: root pytest collects an example package that is not installed, and three Scientist tests use a fake experiment missing the now-required `parent_sha` fact.

**Files:**

- Modify: `pyproject.toml`
- Modify: `tests/test_scientist.py:161-171`

**Consumes:** current test tree and `Experiment.parent_sha`.

**Produces:** one canonical root test command.

- [ ] **Step 1: Verify the RSI checkpoint and clean scope**

```bash
git merge-base --is-ancestor ebe3661 HEAD
git status --short
```

Expected: the first command exits 0; only the pre-existing `.superpowers/` path is untracked before this plan's files.

- [ ] **Step 2: Reproduce both baseline failures**

```bash
python -m pytest -q
python -m pytest tests -q
```

Expected before repair: root collection fails on `examples/tiny_algo_opt/repo/tests/test_correctness.py`; scoped tests report three `_FakeExp.parent_sha` failures.

- [ ] **Step 3: Restrict pytest discovery to the product test tree**

Append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Make `_FakeExp` match the production experiment fact set**

```python
class _FakeExp:
    def __init__(self, rnd, cand, sel, gp, status="completed",
                 metrics=None, finding=None, parent_sha="parent"):
        self.round = rnd
        self.candidate = cand
        self.selected = sel
        self.gate_passed = gp
        self.status = status
        self.metrics = metrics or {}
        self.finding_id = finding
        self.parent_sha = parent_sha
        self.experiment_id = f"r{rnd}c{cand}"
```

- [ ] **Step 5: Verify and commit the clean baseline**

```bash
python -m pytest -q
git add pyproject.toml tests/test_scientist.py
git commit -m "test: restore clean regression baseline"
```

Expected at this checkpoint: `517 passed, 1 skipped`, or the same total with only environment-declared skips.

---

### Task 2: Characterize the five Phase 0 persistence boundaries

**Necessity:** Phase 1 moves parsing code. Compatibility must be locked at durable boundaries, not inferred from unit tests that only assert a few keys.

**Files:**

- Create: `tests/fixtures/phase0/candidate-result.json`
- Create: `tests/fixtures/phase0/proposer-lane-result.json`
- Create: `tests/fixtures/phase0/history-round.json`
- Create: `tests/fixtures/phase0/inflight-round.json`
- Create: `tests/fixtures/phase0/self-review-result.json`
- Create: `tests/test_phase0_characterization.py`
- Modify: `tests/test_candidate_worker.py`

**Consumes:** current worker readers/writers, `Store`, `_InflightJournal`, and RSI result reader.

**Produces:** immutable v0 examples for every later migration task.

- [ ] **Step 1: Add exact worker fixtures**

Use one completed candidate with these facts:

```json
{
  "candidate": 1,
  "experiment_id": "r2c1",
  "proposal": "cache the transform",
  "parent_sha": "parent",
  "sha": "child",
  "status": "COMPLETED",
  "eval_block": "SPEED_MS=90\nCORRECTNESS=PASS",
  "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": true},
  "changed_paths": ["src/cache.cc"],
  "gates": {
    "PATHS": {"passed": true, "detail": ""},
    "EVAL_COMMANDS": {"passed": true, "detail": ""},
    "CORRECTNESS": {"passed": true, "detail": ""}
  },
  "gate_passed": true,
  "eligible": true,
  "selected": false,
  "self_report": null
}
```

Use one completed proposer lane containing `instruction`, `research_target`, `evidence_refs`, and `material_difference`, plus the current `status`, `lane_id`, `round_id`, `outcome`, `reason_kind`, `explanation`, `abstain_reason`, `trace`, and `telemetry` keys.

- [ ] **Step 2: Add exact history, resume, and RSI fixtures**

- `history-round.json`: the current `Store.append_generation()` projection of the candidate above, selected as candidate 1. It must include top-level `round`, `parent_sha`, `selected_candidate`, `selected_sha`, `proposal`, `metrics`, `changed_paths`, `base_sha`, `candidates`, and `telemetry`. The candidate row must include `selected: true` and `telemetry: {}` but omit `self_report`.
- `inflight-round.json`: round 2, parent `parent`, the proposal's current three fields, and one submitted job containing `candidate_id`, `job_id`, `attempt`, `state`, `note`, `worktree_id`, and `result_dir`.
- `self-review-result.json`: a valid `mode: "self"` KEEP result with `contract_version`, incumbent self SHA, diagnosis, keep reason, five-round commitment, null change, and `abstained: false`.

- [ ] **Step 3: Write direct characterization tests**

Create a fixture loader and test the current public boundaries:

```python
FIXTURES = Path(__file__).parent / "fixtures" / "phase0"
SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_history_round_shape(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=SCHEMA)
    store.append_generation(
        2,
        parent_sha="parent",
        selected_candidate=1,
        selected_sha="child",
        candidates=[load("candidate-result.json")],
    )
    assert store.history() == [load("history-round.json")]


def test_inflight_round_shape(tmp_path: Path):
    expected = load("inflight-round.json")
    journal = _InflightJournal(
        tmp_path / "inflight_round.json",
        {key: value for key, value in expected.items() if key != "jobs"},
    )
    journal.save(expected["jobs"])
    assert _load_inflight(tmp_path) == expected
```

Write the lane and self-review fixtures to `result.json`, then assert `read_lane_result()` and `read_self_review_result()` return exact equality. Align the existing deterministic completed-candidate test with the candidate fixture and assert full equality.

- [ ] **Step 4: Verify no production change and commit Phase 0**

```bash
python -m pytest -q tests/test_phase0_characterization.py tests/test_candidate_worker.py
python -m pytest -q
git add tests/fixtures/phase0 tests/test_phase0_characterization.py tests/test_candidate_worker.py
git commit -m "test: freeze phase zero protocols"
```

Expected: all tests pass. If a fixture differs, correct the fixture to the current producer; do not modify production code in this task.

---

### Task 3: Define the Host proposer boundary

**Necessity:** The proposer is physically separate, but Host code still returns proposer-owned `ResearchProposal` and `ProposerResult` types.

**Files:**

- Create: `simpleloop/stages/__init__.py`
- Create: `simpleloop/stages/proposer.py`
- Modify: `simpleloop/execution/base.py`
- Modify: `simpleloop/execution/proposer_lanes.py`
- Modify: `simpleloop/execution/local.py`
- Modify: `simpleloop/execution/hepjob.py`
- Modify: `simpleloop/loop.py`
- Create: `tests/test_host_proposer_contract.py`
- Modify: `tests/test_proposer_lanes.py`
- Modify: `tests/test_parallel_candidates.py`

**Consumes:** `ProposerRequest` and raw lane worker payloads.

**Produces:** `ProposalBatch` containing only Host-relevant proposal facts.

- [ ] **Step 1: Write red tests for a small, frozen contract**

```python
def test_host_proposer_contract_is_small_and_frozen():
    proposal = Proposal("try cache", ("src/a.cc:10",))
    request = ProposerRequest(3, "make it faster", "abc")
    batch = ProposalBatch((proposal,))
    assert request.incumbent_sha == "abc"
    assert batch.abstention is None
    assert not hasattr(proposal, "research_target")
    assert not hasattr(proposal, "material_difference")
    with pytest.raises(FrozenInstanceError):
        proposal.instruction = "mutate"


def test_empty_batch_has_explicit_abstention():
    batch = ProposalBatch(
        (), Abstention("no useful experiment", "missing profile")
    )
    assert batch.abstained is True
    assert batch.abstention.blocking_unknown == "missing profile"
```

Run:

```bash
python -m pytest -q tests/test_host_proposer_contract.py
```

Expected: import failure because the module does not exist.

- [ ] **Step 2: Implement the live contract only**

```python
@dataclass(frozen=True)
class ProposerRequest:
    round_id: int
    goal: str
    incumbent_sha: str


@dataclass(frozen=True)
class Proposal:
    instruction: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise ValueError("proposal instruction must not be empty")


@dataclass(frozen=True)
class Abstention:
    reason: str
    blocking_unknown: str | None = None


@dataclass(frozen=True)
class ProposalBatch:
    proposals: tuple[Proposal, ...]
    abstention: Abstention | None = None
    telemetry: Mapping[str, object] = field(default_factory=dict)
    trace: Mapping[str, object] = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        return not self.proposals


def decode_lane_proposals(rows: Sequence[Mapping[str, object]]) -> tuple[Proposal, ...]:
    return tuple(
        Proposal(
            instruction=str(row["instruction"]),
            evidence_refs=tuple(str(ref) for ref in row.get("evidence_refs") or ()),
        )
        for row in rows
    )
```

- [ ] **Step 3: Convert lane collection and backend signatures**

Remove imports of `proposal_from_dict` and `proposer.scientist` from `execution/proposer_lanes.py`. Decode each completed lane once and return:

```python
return ProposalBatch(
    proposals=tuple(proposals),
    abstention=(
        Abstention("all lanes abstained/blocked/errored")
        if not proposals else None
    ),
    telemetry={"lanes": lane_telemetries},
    trace={"lanes": lane_traces},
)
```

Change abstract, Local, and HEPJob signatures together:

```python
def run_proposer_lanes(self, request: ProposerRequest) -> ProposalBatch:
    raise NotImplementedError
```

Use `request.round_id` and `request.incumbent_sha` to build the unchanged lane workspace and manifest.

- [ ] **Step 4: Remove proposer-owned types from the loop**

Static mode returns `ProposalBatch((Proposal(proposal_text),))`. Normal mode calls:

```python
request = ProposerRequest(
    round_id=round_id,
    goal=str(ctx.cfg["goal"]),
    incumbent_sha=parent_sha,
)
proposal_batch = ctx.execution_backend.run_proposer_lanes(request)
```

Update trace/handoff helpers to accept `ProposalBatch`. Host artifacts write only `instruction` and `evidence_refs`. Do not catch proposer package exception classes in `loop.py`; the subprocess boundary already returns Host-level failures.

- [ ] **Step 5: Verify and commit the proposer firewall**

```bash
python -m pytest -q tests/test_host_proposer_contract.py tests/test_proposer_lanes.py tests/test_parallel_candidates.py -k "proposal or lane or abstain"
rg -n "(^|from |import )proposer" simpleloop/loop.py simpleloop/execution/proposer_lanes.py
git add simpleloop/stages simpleloop/execution/base.py simpleloop/execution/proposer_lanes.py simpleloop/execution/local.py simpleloop/execution/hepjob.py simpleloop/loop.py tests/test_host_proposer_contract.py tests/test_proposer_lanes.py tests/test_parallel_candidates.py
git commit -m "refactor: define host proposer contract"
```

Expected: tests pass and `rg` returns no matches.

---

### Task 4: Define `CandidateResult` and the worker codec

**Necessity:** The same candidate dictionary currently acts as worker protocol, business state, selection state, telemetry carrier, and history row.

**Files:**

- Create: `simpleloop/candidate.py`
- Create: `simpleloop/persistence/__init__.py`
- Create: `simpleloop/persistence/artifacts.py`
- Create: `tests/test_candidate_contract.py`
- Create: `tests/test_candidate_codec.py`

**Consumes:** characterized v0 candidate JSON.

**Produces:** one frozen candidate model and one strict wire codec.

- [ ] **Step 1: Write red model and codec tests**

```python
def test_candidate_fixture_round_trips():
    raw = load_phase0("candidate-result.json")
    result = decode_candidate_result(raw)
    assert result.status is CandidateStatus.COMPLETED
    assert result.parent_sha == "parent"
    assert result.artifact.sha == "child"
    assert result.metrics["SPEED_MS"] == 90.0
    assert not hasattr(result, "selected")
    assert encode_candidate_result(result) == raw


@pytest.mark.parametrize("key", ["candidate", "proposal", "parent_sha", "status"])
def test_candidate_decoder_rejects_missing_required_key(key):
    raw = load_phase0("candidate-result.json")
    raw.pop(key)
    with pytest.raises(ProtocolError, match=key):
        decode_candidate_result(raw)
```

Also reject unknown status, non-list paths, non-object metrics/gates, malformed gate rows, and non-boolean `gate_passed`/`eligible`.

- [ ] **Step 2: Implement the minimal final candidate facts**

```python
class CandidateStatus(str, Enum):
    COMPLETED = "COMPLETED"
    GATE_REJECTED = "GATE_REJECTED"
    EXECUTOR_FAILED = "EXECUTOR_FAILED"
    NO_CHANGE = "NO_CHANGE"
    EVAL_FAILED = "EVAL_FAILED"
    WORKER_FAILED = "WORKER_FAILED"
    BASELINE = "BASELINE"


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str | None = None
    output: str = ""
    self_report: Mapping[str, object] | None = None


@dataclass(frozen=True)
class CandidateArtifact:
    parent_sha: str
    sha: str
    changed_paths: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True)
class EvaluationResult:
    text: str
    metrics: Mapping[str, object] = field(default_factory=dict)
    returncodes: tuple[int, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class GateResult:
    passed: bool | None
    detail: str = ""


@dataclass(frozen=True)
class GateDecision:
    results: Mapping[str, GateResult]
    passed: bool
    eligible: bool


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: int
    experiment_id: str
    proposal: Proposal
    parent_sha: str
    status: CandidateStatus
    execution: ExecutionResult
    artifact: CandidateArtifact | None
    evaluation: EvaluationResult | None
    gate: GateDecision
    usage: tuple[Mapping[str, object], ...] = ()
    telemetry: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.artifact and self.artifact.parent_sha != self.parent_sha:
            raise ValueError("artifact parent_sha differs from candidate parent_sha")

    @property
    def sha(self) -> str | None:
        return self.artifact.sha if self.artifact else None

    @property
    def metrics(self) -> Mapping[str, object]:
        return self.evaluation.metrics if self.evaluation else {}

    @property
    def eligible(self) -> bool:
        return self.gate.eligible
```

`parent_sha` is deliberately first-class because executor failure and no-change have no artifact but still require lineage in history.

- [ ] **Step 3: Implement strict `decode_candidate_result()`**

In `simpleloop/persistence/artifacts.py`, define `ProtocolError(RuntimeError)` and enforce:

- required keys and exact status enum;
- integer candidate ID, string proposal/parent SHA, optional string child SHA;
- tuple conversion for paths and gate rows;
- artifact whenever child SHA exists, including eval failure and baseline;
- evaluation for completed, rejected, eval-failed, and baseline statuses;
- execution reason from failure `eval_block`; no-change reason from current worker semantics;
- `self_report` and telemetry must be mappings when present;
- default experiment ID only for old payloads that omit it.

The function returns `CandidateResult`; it never returns a partially validated dictionary.

- [ ] **Step 4: Implement exact `encode_candidate_result()`**

```python
def encode_candidate_result(
    result: CandidateResult,
    *,
    selected: bool = False,
) -> dict[str, object]:
    eval_block = result.evaluation.text if result.evaluation else ""
    if (
        not eval_block
        and result.status in {
            CandidateStatus.EXECUTOR_FAILED,
            CandidateStatus.WORKER_FAILED,
        }
    ):
        reason = result.execution.reason or ""
        eval_block = reason if reason.startswith("[") else f"[loop failure] {reason[:200]}"
    return {
        "candidate": result.candidate_id,
        "experiment_id": result.experiment_id,
        "proposal": result.proposal.instruction,
        "parent_sha": result.parent_sha,
        "sha": result.sha,
        "status": result.status.value,
        "eval_block": eval_block,
        "metrics": dict(result.metrics),
        "changed_paths": [
            path.as_posix()
            for path in (result.artifact.changed_paths if result.artifact else ())
        ],
        "gates": {
            name: {"passed": gate.passed, "detail": gate.detail}
            for name, gate in result.gate.results.items()
        },
        "gate_passed": result.gate.passed,
        "eligible": result.gate.eligible,
        "selected": selected,
        "self_report": (
            dict(result.execution.self_report)
            if result.execution.self_report is not None else None
        ),
    }
```

Never encode `usage` into business `result.json`; it remains in `usage.json`.

- [ ] **Step 5: Verify the codec and commit**

```bash
python -m pytest -q tests/test_candidate_contract.py tests/test_candidate_codec.py tests/test_phase0_characterization.py
git add simpleloop/candidate.py simpleloop/persistence tests/test_candidate_contract.py tests/test_candidate_codec.py
git commit -m "refactor: add typed candidate result codec"
```

---

### Task 5: Migrate candidate producers and backends

**Necessity:** A typed model is useful only when every live producer returns it and every remote boundary decodes it exactly once.

**Files:**

- Modify: `simpleloop/candidate_worker.py`
- Modify: `simpleloop/execution/base.py`
- Modify: `simpleloop/execution/local.py`
- Modify: `simpleloop/execution/hepjob.py`
- Modify: `simpleloop/loop.py:780-895`
- Modify: `tests/test_candidate_worker.py`
- Modify: `tests/test_hepjob_backend.py`
- Modify: `tests/test_parallel_candidates.py`

**Consumes:** `CandidateSpec` and current runtime dependencies.

**Produces:** `CandidateResult` in memory and unchanged v0 JSON on disk.

- [ ] **Step 1: Convert worker tests from keys to attributes and verify red**

```python
assert result.status is CandidateStatus.COMPLETED
assert result.sha == "def456"
assert result.parent_sha == "abc123"
assert result.gate.passed is True
assert result.eligible is True
assert result.gate.results["EVAL_COMMANDS"].passed is True
assert result.metrics["SPEED_MS"] == 100.0
```

Run `python -m pytest -q tests/test_candidate_worker.py`; expect failures because worker functions still return dictionaries.

- [ ] **Step 2: Make every worker terminal branch construct one typed result**

Change return types together:

```python
def run_candidate(deps: CandidateDeps, spec: CandidateSpec) -> CandidateResult:
def _candidate_result(spec: CandidateSpec, result: ExecResult, **facts) -> CandidateResult:
def _run_baseline_eval(deps: CandidateDeps, spec: CandidateSpec, cfg: dict) -> CandidateResult:
def candidate_failure(candidate_id: int, spec: CandidateSpec,
                      reason: str, parent_sha: str, **facts) -> CandidateResult:
```

Map facts without reclassification:

- executor failure: no artifact/evaluation, failed execution, false gate;
- no change: no artifact/evaluation, retained executor reason, unknown downstream gates;
- eval launch failure: committed artifact, evaluation error, eval-command gate false;
- gate rejection: committed artifact/evaluation, false gate;
- completed/baseline: artifact/evaluation/gate/eligibility retained.

- [ ] **Step 3: Encode only at the standalone worker boundary**

`write_result()` accepts `CandidateResult`, calls `encode_candidate_result()`, and retains the existing atomic order: result temp, `os.replace`, optional sidecar temp/replace, `_FINISHED` last. Update the CLI catch-all to build `candidate_failure()` and update its mocked success test to return a minimal real `CandidateResult`.

- [ ] **Step 4: Decode HEPJob result once and preserve telemetry immutably**

```python
@staticmethod
def _read_result(job: _Job) -> CandidateResult:
    raw = json.loads(
        (job.result_dir / "result.json").read_text(encoding="utf-8")
    )
    return decode_candidate_result(raw)
```

Because `_Job` is reused for proposer lanes, annotate its result as `CandidateResult | dict | None` and narrow it inside candidate/lane collectors. In candidate `_collect()`:

```python
result = replace(
    job.result,
    usage=tuple(meta.get("usage") or ()),
)
```

Keep malformed/missing result classified as infrastructure failure and excluded from history.

- [ ] **Step 5: Make local execution and telemetry finalization immutable**

Backend candidate methods return `tuple[CandidateResult, ...]`. Replace loop mutation with:

```python
def _finalize_candidates(
    ctx: RunContext,
    candidates: tuple[CandidateResult, ...],
) -> tuple[CandidateResult, ...]:
    finalized = []
    for candidate in candidates:
        for usage in candidate.usage:
            ctx.telemetry.record_usage(usage)
        finalized.append(replace(
            candidate,
            usage=(),
            telemetry=ctx.telemetry.snapshot(persist=True),
        ))
    return tuple(finalized)
```

- [ ] **Step 6: Verify worker/backend parity and commit**

```bash
python -m pytest -q tests/test_candidate_worker.py tests/test_hepjob_backend.py tests/test_parallel_candidates.py
python -m pytest -q tests/test_phase0_characterization.py
git add simpleloop/candidate_worker.py simpleloop/execution/base.py simpleloop/execution/local.py simpleloop/execution/hepjob.py simpleloop/loop.py tests/test_candidate_worker.py tests/test_hepjob_backend.py tests/test_parallel_candidates.py
git commit -m "refactor: return typed candidate results"
```

Expected: Local and HEPJob return the same type; candidate fixture stays equal.

---

### Task 6: Move selection and persistence onto `RoundResult`

**Necessity:** Selection belongs to a round, not a mutable `selected` field on each candidate. History is a projection of one terminal round.

**Files:**

- Create: `simpleloop/stages/selector.py`
- Create: `simpleloop/round.py`
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/harness/store.py`
- Create: `tests/test_round_contract.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_views_and_parse.py`
- Modify: `tests/test_phase0_characterization.py`
- Modify: Store callers in plot/export/provenance tests

**Consumes:** `ProposalBatch` and typed candidates.

**Produces:** `Selection`, `RoundResult`, and one legacy-compatible history projection.

- [ ] **Step 1: Write red selector tests for all current policies**

Test best eligible candidate, deterministic candidate-ID tie break, no eligible candidate, no improvement, and static mode accepting a gate-valid regression. Assert candidates have no `selected` attribute.

- [ ] **Step 2: Implement the config-free selector**

```python
@dataclass(frozen=True)
class Selection:
    candidate_id: int | None
    sha: str | None
    reason: str


def select_candidate(
    *,
    candidates: tuple[CandidateResult, ...],
    objective_key: str,
    lower_is_better: bool,
    incumbent_value: float | None,
    require_improvement: bool = True,
) -> Selection:
    eligible = []
    for candidate in candidates:
        value = candidate.metrics.get(objective_key)
        numeric = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
        if candidate.eligible and candidate.sha and numeric:
            eligible.append((candidate, float(value)))
    if not eligible:
        return Selection(None, None, "no_eligible_candidate")
    direction = 1 if lower_is_better else -1
    winner, winner_value = min(
        eligible,
        key=lambda item: (direction * item[1], item[0].candidate_id),
    )
    if require_improvement and incumbent_value is not None:
        improved = (
            winner_value < incumbent_value
            if lower_is_better else winner_value > incumbent_value
        )
        if not improved:
            return Selection(None, None, "no_improvement")
    return Selection(winner.candidate_id, winner.sha, "selected")
```

- [ ] **Step 3: Define the only round persistence input**

```python
@dataclass(frozen=True)
class RoundResult:
    round_id: int
    parent_sha: str
    proposals: ProposalBatch
    candidates: tuple[CandidateResult, ...]
    selection: Selection
    telemetry: Mapping[str, object] = field(default_factory=dict)

    @property
    def next_sha(self) -> str:
        return self.selection.sha or self.parent_sha
```

- [ ] **Step 4: Replace `Store.append_generation()` with `append_round()`**

`append_round(result: RoundResult)` must:

- call `encode_candidate_result(candidate, selected=(candidate.candidate_id == result.selection.candidate_id))` once per candidate;
- cap `eval_block` to `history_eval_cap`;
- omit worker-only `self_report` exactly as the current Store does;
- add `CandidateResult.telemetry` to each history candidate;
- derive top-level proposal, metrics, paths, and base SHA from `Selection`;
- derive abstention and deliberation telemetry from `ProposalBatch`;
- append exactly one JSON line.

Keep `history()`, `eligible(dict, schema)`, and `best_candidate(dict history, schema)` as explicitly legacy persistence/reporting readers. Current-round selection must not call them.

- [ ] **Step 5: Build one typed terminal result in the existing loop**

Both fresh and resume paths maintain a typed `ProposalBatch`. Resume ignores legacy proposer-only fields and reads `instruction`/`evidence_refs`. After execution:

```python
candidates = _finalize_candidates(ctx, tuple(candidates))
objective = ctx.metrics_schema["objective"]
prior_value = (prior_metrics or {}).get(objective["key"])
selection = select_candidate(
    candidates=candidates,
    objective_key=objective["key"],
    lower_is_better=objective["lower_is_better"],
    incumbent_value=(
        float(prior_value)
        if isinstance(prior_value, (int, float))
        and not isinstance(prior_value, bool)
        else None
    ),
    require_improvement=static_proposals is None,
)
round_result = RoundResult(
    round_id=round_id,
    parent_sha=parent_sha,
    proposals=proposal_batch,
    candidates=candidates,
    selection=selection,
    telemetry=ctx.telemetry.snapshot(persist=True),
)
ctx.store.append_round(round_result)
parent_sha = round_result.next_sha
```

Find a selected candidate by matching `selection.candidate_id`; never mutate a candidate.

- [ ] **Step 6: Keep the Phase 0 history fixture exact and commit**

Update characterization to decode its candidate fixture, create `RoundResult`, call `append_round()`, and compare exact history JSON. Add an abstention projection test.

```bash
python -m pytest -q tests/test_round_contract.py tests/test_parallel_candidates.py tests/test_views_and_parse.py tests/test_phase0_characterization.py tests/test_plot.py tests/test_provenance_export_lock.py
git add simpleloop/stages/selector.py simpleloop/round.py simpleloop/loop.py simpleloop/harness/store.py tests/test_round_contract.py tests/test_parallel_candidates.py tests/test_views_and_parse.py tests/test_phase0_characterization.py tests/test_plot.py tests/test_provenance_export_lock.py
git commit -m "refactor: persist typed round results"
```

---

### Task 7: Enforce and verify the Phase 1 boundary

**Necessity:** Functional tests do not prevent proposer imports or candidate dictionary indexing from leaking back into business code.

**Files:**

- Create: `tests/test_architecture_boundaries.py`
- Modify: touched docstrings/type annotations only if checks expose stale claims

**Consumes:** completed Phase 1 source tree.

**Produces:** executable architectural guards and the final checkpoint.

- [ ] **Step 1: Add the proposer import firewall**

```python
def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_host_pipeline_does_not_import_proposer_package():
    root = Path(__file__).parents[1]
    for relative in (
        "simpleloop/loop.py",
        "simpleloop/execution/proposer_lanes.py",
        "simpleloop/stages/proposer.py",
    ):
        imports = imported_modules(root / relative)
        assert not any(
            name == "proposer" or name.startswith("proposer.")
            for name in imports
        ), relative
```

Do not scan `proposer_lane_worker.py`; it is the intentional adapter executing proposer code.

- [ ] **Step 2: Add a current-round dictionary-index guard**

```python
PIPELINE_FUNCTIONS = {
    "_run_locked",
    "_finalize_candidates",
    "_run_candidates",
    "_run_candidate_guarded",
    "_run_one_candidate",
    "_candidate_failure",
    "_print_round_performance",
}
CANDIDATE_NAMES = {"candidate", "winner", "first", "best"}


def test_current_round_pipeline_does_not_index_candidate_dicts():
    root = Path(__file__).parents[1]
    tree = ast.parse((root / "simpleloop/loop.py").read_text(encoding="utf-8"))
    violations = []
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in PIPELINE_FUNCTIONS
    ):
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in CANDIDATE_NAMES
            ):
                violations.append((function.name, node.lineno, node.value.id))
    assert violations == []
```

Legacy persisted-history readers `_resume_chain()` and `_summary()` are intentionally outside this guard until the reporting/history migration.

- [ ] **Step 3: Run structural and focused protocol checks**

```bash
python -m pytest -q tests/test_architecture_boundaries.py tests/test_phase0_characterization.py tests/test_candidate_codec.py tests/test_host_proposer_contract.py tests/test_round_contract.py
```

- [ ] **Step 4: Run full verification and inspect remaining dictionary boundaries**

```bash
python -m pytest -q
rg -n "ResearchProposal|ProposerResult|research_target|material_difference" simpleloop/loop.py simpleloop/execution simpleloop/stages
rg -n -- "-> (dict|list\[dict\])|: (dict|list\[dict\])" simpleloop/loop.py simpleloop/candidate_worker.py simpleloop/execution/base.py simpleloop/execution/local.py
rg -n "CandidateResult|ProposalBatch|RoundResult|Selection" simpleloop tests
git diff --check
git status --short
```

Expected:

- full suite passes;
- proposer-owned types are absent from Host pipeline modules;
- migrated live result boundaries have no dictionary annotations;
- remaining dictionaries are config, telemetry, journal payloads, codecs, or legacy history/reporting;
- each new contract has a production consumer and tests;
- `.superpowers/` is not staged.

- [ ] **Step 5: Commit the Phase 1 checkpoint**

```bash
git add tests/test_architecture_boundaries.py docs/superpowers/plans/2026-08-14-simpleloop-phase0-phase1-typed-contracts.md
git commit -m "test: enforce typed kernel boundaries"
```

---

## Explicit Non-Goals

Stop after this checkpoint. Do not:

- extract the candidate pipeline (Phase 2);
- redesign Workspace/Apptainer isolation (Phase 3);
- unify Local/HEPJob supervision or worker envelopes (Phase 4);
- create `run_round()`, `run_loop()`, or remove `RunContext` (Phase 5);
- split `self_repo.py` or replace RSI adoption/state (Phase 6);
- introduce the new config schema or delete all legacy directories (Phase 7).

The necessary result is narrower: the current Kernel still runs, but its live business values are typed and every compatibility dictionary has one named boundary.

## Final Review Checklist

- [ ] Root test baseline passes.
- [ ] Five durable formats are characterized.
- [ ] Proposer package is isolated behind its worker adapter.
- [ ] Candidate result is one frozen in-memory model.
- [ ] Selection is immutable round state.
- [ ] History is projected from `RoundResult`.
- [ ] Local, HEPJob, resume, RSI, plot, and export tests pass.
- [ ] No unused future-phase framework was added.
- [ ] Every task has one focused, revertible commit.
