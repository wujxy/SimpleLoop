# Standalone Proposer Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `simpleloop propose`, a standalone runner for the complete Generator + Cognitive Proposer pipeline that writes proposals for human review without constructing an Executor or running evaluation.

**Architecture:** A new `simpleloop/proposer_harness.py` module is a thin adapter over the existing `ProposerOrchestrator.run()` API. It owns config/runtime/workspace setup, optional historical-memory selection, result serialization, deterministic Python-side seeding, and cleanup; `simpleloop/cli.py` only parses arguments and presents errors.

**Tech Stack:** Python 3.9+, argparse, dataclasses, pathlib, existing SimpleLoop config/runtime/workspace/memory/orchestrator modules, pytest.

## Global Constraints

- Reuse the production `ProposerOrchestrator`; do not copy Generator or Cognitive logic.
- Do not construct an Executor, execution backend, Store, evaluator, or Gate runner.
- `--from-run` is read-only and must never append history or allocate findings.
- Preserve normal `simpleloop run` behavior and existing public result types.
- Write tests before production code and verify each test fails for the intended missing behavior.
- Keep the MVP to one runner module, one CLI branch, and targeted tests.

---

### Task 1: Result projection and history/base resolution

**Files:**

- Create: `tests/test_proposer_harness.py`
- Create: `simpleloop/proposer_harness.py`

**Interfaces:**

- Produces: `ProposerHarnessError(RuntimeError)`
- Produces: `_proposal_to_dict(proposal: ResearchProposal) -> dict`
- Produces: `_render_proposals_markdown(result: dict) -> str`
- Produces: `_history_state(history_dir: Path, baseline_sha: str) -> tuple[str, int]`

- [ ] **Step 1: Write failing projection and history tests**

Add tests that exercise the real proposal dataclasses and current history
schema:

```python
def test_proposal_to_dict_preserves_new_target():
    proposal = ResearchProposal(
        instruction="change the data layout",
        research_target=NewFindingTarget(
            question="Can SoA remove repeated gathers?",
            mechanisms=("SoA",),
            code_regions=("src/fcn.cc",),
        ),
        evidence_refs=("source:src/fcn.cc:42",),
        material_difference="moves ownership across the interface",
    )
    assert harness._proposal_to_dict(proposal) == {
        "instruction": "change the data layout",
        "research_target": {
            "mode": "new",
            "question": "Can SoA remove repeated gathers?",
            "mechanisms": ["SoA"],
            "code_regions": ["src/fcn.cc"],
        },
        "evidence_refs": ["source:src/fcn.cc:42"],
        "material_difference": "moves ownership across the interface",
    }


def test_history_state_keeps_last_selected_sha(tmp_path):
    rows = [
        _history_row(0, parent="base", selected="winner"),
        _history_row(1, parent="winner", selected=None),
    ]
    history_dir = tmp_path / "source-run"
    history_dir.mkdir()
    (history_dir / "history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    assert harness._history_state(history_dir, "base") == ("winner", 2)
```

Also cover existing targets, empty history, malformed history, and Markdown
numbering/content.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest -q tests/test_proposer_harness.py
```

Expected: collection fails because `simpleloop.proposer_harness` does not
exist.

- [ ] **Step 3: Implement minimal projection and history helpers**

Create `simpleloop/proposer_harness.py` with imports from
`simpleloop.memory.models` and `simpleloop.harness.memory`. Serialize the two
research-target variants explicitly:

```python
def _target_to_dict(target) -> dict:
    if isinstance(target, ExistingFindingTarget):
        return {"mode": "existing", "finding_id": target.finding_id}
    if isinstance(target, NewFindingTarget):
        return {
            "mode": "new",
            "question": target.question,
            "mechanisms": list(target.mechanisms),
            "code_regions": list(target.code_regions),
        }
    raise TypeError(f"unsupported research target: {type(target).__name__}")


def _history_state(history_dir: Path, baseline_sha: str) -> tuple[str, int]:
    rows = memory.read_history(history_dir / "history.jsonl")
    base_sha = baseline_sha
    for row in rows:
        if row.get("selected_sha"):
            base_sha = str(row["selected_sha"])
    current_round = max((int(row["round"]) for row in rows), default=-1) + 1
    return base_sha, current_round
```

Render `proposals.md` solely from the authoritative result dictionary.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
pytest -q tests/test_proposer_harness.py
```

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add simpleloop/proposer_harness.py tests/test_proposer_harness.py
git commit -m "test: define standalone proposer result contract"
```

---

### Task 2: Complete standalone proposer runner

**Files:**

- Modify: `tests/test_proposer_harness.py`
- Modify: `simpleloop/proposer_harness.py`

**Interfaces:**

- Produces: `ProposerHarnessSummary`
- Produces: `run_proposer(config_path, output_dir, *, from_run=None, seed=None) -> ProposerHarnessSummary`
- Consumes: `config.load`, `ApptainerRuntime`, `Workspace`, `MemoryService`, `build_chat_model`, `ProposerOrchestrator`, and `views.gate_block`

- [ ] **Step 1: Write failing fresh-mode integration test**

Use monkeypatched fake runtime, workspace, model builder, memory service, and
orchestrator. Assert the complete orchestration arguments and artifacts:

```python
def test_run_proposer_invokes_complete_pipeline_without_executor(
    monkeypatch, tmp_path,
):
    calls = _install_fakes(monkeypatch, tmp_path)
    summary = harness.run_proposer(
        tmp_path / "task.yaml",
        tmp_path / "out",
        seed=42,
    )
    kwargs = calls["orchestrator_run"]
    assert kwargs["goal"] == "make it faster"
    assert kwargs["base_sha"] == "baseline-sha"
    assert kwargs["current_round"] == 0
    assert kwargs["candidates_per_round"] == 4
    assert kwargs["gen_steps"] == 216
    assert kwargs["cognitive_steps"] == 148
    assert calls["workspace_removed"] == ["proposer-test"]
    assert summary.proposal_count == 1
    result = json.loads(summary.result_path.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert result["proposals"][0]["instruction"] == "try SoA"
```

The fake config intentionally has `roles.executor = None`. Patch only symbols
in `simpleloop.proposer_harness`; any accidental call into loop Executor setup
therefore fails the test.

- [ ] **Step 2: Write failing existing-run and safety tests**

Add tests that verify:

- `--from-run` supplies the historical `MemoryService` and history directory
  to the orchestrator while Workspace remains under the output directory;
- the latest selected SHA and next round are used;
- source-run `history.jsonl` bytes are unchanged;
- the configured baseline is used when historical rows have no selection;
- an existing completed `result.json` is refused;
- seeded execution restores `random.getstate()`;
- proposer failure writes `status: failed`, returns the original exception,
  and still removes the worktree;
- abstention serializes with an empty proposal list.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
pytest -q tests/test_proposer_harness.py
```

Expected: tests fail because `run_proposer` and `ProposerHarnessSummary` are
missing.

- [ ] **Step 4: Implement minimal runner**

Implement the following exact public types and function signature; the body
performs the ordered operations immediately below:

```python
@dataclass(frozen=True)
class ProposerHarnessSummary:
    result_path: Path
    report_path: Path
    proposal_count: int
    abstained: bool
    base_sha: str


def run_proposer(
    config_path: str | Path,
    output_dir: str | Path,
    *,
    from_run: str | Path | None = None,
    seed: int | None = None,
) -> ProposerHarnessSummary:
    """Run one complete Proposer attempt and persist review artifacts."""
```

The implementation order is:

1. resolve paths and refuse an existing completed result;
2. create the output directory;
3. load config and require `roles.researcher`;
4. create/preflight `ApptainerRuntime` with `run_dir=output_dir`;
5. create/setup `Workspace` under `output_dir`;
6. resolve configured baseline, optional historical base SHA, and round;
7. add one `proposer-test` worktree;
8. create `MemoryService` using the history source directory;
9. capture model usage events through `usage_observer`;
10. call `ProposerOrchestrator.run()` with exactly the production arguments;
11. write `result.json` atomically and derive `proposals.md`;
12. restore random state and remove the worktree in `finally`.

Use a small `_write_json(path, value)` helper that writes UTF-8 JSON to a
sibling temporary file and replaces the destination. On failure, best-effort
write a failed result after cleanup state has been determined, then re-raise.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
pytest -q tests/test_proposer_harness.py
```

Expected: all standalone runner tests pass.

- [ ] **Step 6: Run neighboring proposer tests**

Run:

```bash
pytest -q tests/test_generator.py tests/test_orchestrator.py tests/test_cognitive_element.py
```

Expected: all tests pass; existing proposer behavior is unchanged.

- [ ] **Step 7: Commit Task 2**

```bash
git add simpleloop/proposer_harness.py tests/test_proposer_harness.py
git commit -m "feat: add standalone proposer runner"
```

---

### Task 3: CLI entry point and user-facing behavior

**Files:**

- Modify: `simpleloop/cli.py`
- Modify: `tests/test_proposer_harness.py`
- Modify: `docs/README.md`

**Interfaces:**

- Consumes: `run_proposer(config_path, output_dir, *, from_run=None, seed=None)`
- Produces CLI: `simpleloop propose --config PATH --output-dir PATH [--from-run PATH] [--seed INT]`

- [ ] **Step 1: Write failing CLI success test**

Patch `simpleloop.proposer_harness.run_proposer`, call `cli.main` with the argument list below, and
assert exact argument forwarding and concise output:

```python
def test_cli_propose_forwards_arguments(monkeypatch, tmp_path, capsys):
    captured = {}
    monkeypatch.setattr(
        harness,
        "run_proposer",
        lambda config, output, **kwargs: _fake_summary(
            tmp_path, captured, config, output, kwargs
        ),
    )
    cli.main([
        "propose", "--config", "task.yaml",
        "--output-dir", "trial", "--from-run", "old-run",
        "--seed", "42",
    ])
    assert captured == {
        "config": "task.yaml",
        "output": "trial",
        "from_run": "old-run",
        "seed": 42,
    }
    assert "1 proposal(s)" in capsys.readouterr().out
```

- [ ] **Step 2: Write failing CLI error test**

Patch the runner to raise `ProposerHarnessError("bad history")`; assert
`SystemExit(1)` and `Proposer error: bad history` on stderr.

- [ ] **Step 3: Run CLI tests and verify RED**

Run:

```bash
pytest -q tests/test_proposer_harness.py -k cli
```

Expected: tests fail because the `propose` parser/branch does not exist.

- [ ] **Step 4: Add the CLI parser and dispatch branch**

Register the parser before `parse_args`:

```python
propose = sub.add_parser(
    "propose",
    help="Run the complete Proposer pipeline without Executor or evaluation.",
)
propose.add_argument("--config", required=True)
propose.add_argument("--output-dir", required=True)
propose.add_argument("--from-run")
propose.add_argument("--seed", type=int)
```

Dispatch to `proposer_harness.run_proposer`, catch
`ConfigError`, `RuntimePreflightError`, `ModelError`, `ProposerError`,
`WorkspaceError`, `ProposerHarnessError`, and `ValueError`, print one concise
stderr line, and exit 1. Do not call any normal-loop setup function.

- [ ] **Step 5: Add concise README usage**

Document fresh and `--from-run` examples, state that only Python-side lens
scheduling is seeded, and list `result.json` plus `proposals.md`.

- [ ] **Step 6: Run targeted tests and verify GREEN**

Run:

```bash
pytest -q tests/test_proposer_harness.py tests/test_initialize.py tests/test_config_execution.py
```

Expected: all tests pass.

- [ ] **Step 7: Run full test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 8: Validate CLI help without external services**

Run:

```bash
python -m simpleloop.cli propose --help
```

Expected: exit 0 and output lists `--config`, `--output-dir`, `--from-run`, and
`--seed`.

- [ ] **Step 9: Commit Task 3**

```bash
git add simpleloop/cli.py simpleloop/proposer_harness.py tests/test_proposer_harness.py docs/README.md
git commit -m "feat: expose standalone proposer CLI"
```
