# Open Workspace and External Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Replace source-path allowlists with a harness-materialized mutable production workspace, evaluated through an immutable external eval.sh /work contract.

**Architecture:** The task configuration contains only harness-private workspace bootstrap and evaluator data. The harness archives the declared production entries at a seed revision into a new internal Git repository, then creates all baseline and candidate worktrees from that package. Proposer and executor only see /work. The evaluator is separately mounted and invoked as bash /evaluator/eval.sh /work plus configured arguments.

**Tech Stack:** Python 3.11, YAML, Git archive/init/worktree, Apptainer, pytest.

## Global Constraints

- Remove safety.editable_paths and safety.frozen_paths from configuration, prompts, and candidate acceptance.
- workspace is harness-only metadata. It must never appear in an agent prompt.
- The package may contain only user-declared mutable production files. Evaluator code, golden references, benchmarks, baselines, and test assets must not be copied into it.
- Proposer mounts /work read-only and /scratch read-write. It receives only goal, gate meanings, and generic workspace ownership.
- Executor mounts only /work read-write. It can create, delete, move, and replace all package files, including CMake/build layout.
- Evaluator mounts /work plus its own immutable directory and evaluator-only read-only binds.
- Internal Git remains for lineage, history, and diffs only. It is not an agent capability.
- Keep current objective/gate parsing and local/hepjob behavior.
- Tests precede behavior changes. Run targeted pytest after every task and the full suite at the end.

---

## Target interfaces

### Configuration

~~~yaml
kind: task

task:
  goal: Minimize SPEED_MS while satisfying every configured gate.

workspace:
  seed:
    path: ../../../omilrec-v100-gated
    ref: simpleloop-v100-gated-baseline
  copy:
    - OMILRECV2
    - CMakeLists.txt

evaluation:
  runner: evaluator/eval.sh
  args: [--evtmax, "100"]
  binds: [/cvmfs, /data/juno]
  metrics:
    objective: {key: SPEED_MS, lower_is_better: true}
    gates: [...]
~~~

Resolved fields are workspace_seed_path, workspace_seed_ref, workspace_copy, evaluator_runner, evaluator_args, evaluator_binds, existing metrics and existing timeout/output settings. Legacy source and safety are unknown top-level keys and fail validation.

### Runtime capabilities

~~~text
proposer  /work:ro + /scratch:rw, no evaluator bind
executor  /work:rw, no runtime broad binds or evaluator bind
evaluator /work:rw + /evaluator:ro + evaluator-only binds:ro
~~~

### Agent-visible contract

~~~text
Workspace:
The provided workspace contains the complete mutable production artifact.
Its current structure is only the starting implementation, not part of the specification.
All task-specific prior knowledge available to you has been placed in this workspace.

Evaluation:
Success is determined only by the stated goal and gates. Evaluation is external
to the workspace; do not infer structural requirements beyond those criteria.
~~~

The executor additionally receives: You may inspect, create, delete, move, replace, or reorganize anything inside /work.

## File map

| File | Change |
|---|---|
| simpleloop/config.py | Resolve workspace/evaluation config and reject source/safety. |
| simpleloop/initialize.py | Verify seed repo/ref; do not initialize or write the user seed. |
| simpleloop/harness/workspace.py | Materialize copy manifest into private Git package. |
| simpleloop/container/runtime.py | Add executor and evaluator isolated argv builders. |
| simpleloop/roles/agent.py | Launch executor with executor-specific argv. |
| simpleloop/harness/evals.py | Add run_external_eval(runner, args, workspace, runtime, evaluator_binds, ...). |
| simpleloop/roles/executor.py | Remove editable/frozen prompt and diff-path rejection. |
| simpleloop/memory/context.py | Render only goal/gates and generic workspace contract. |
| simpleloop/roles/proposer.py | Rename researcher-facing source mount/context to workspace. |
| simpleloop/roles/research_agent.py, simpleloop/roles/research_tools.py | Read workspace at /work and record workspace evidence. |
| simpleloop/loop.py, simpleloop/candidate_worker.py, simpleloop/execution/local.py | Construct package and use external evaluator for baseline/candidates. |
| scripts/proposer_harness.py | Use package snapshot without forwarding seed/safety metadata to proposer. |
| examples/omilrec-v100-opt/evaluator/eval.sh | Immutable OMILREC eval adapter accepting /work as argv[1]. |
| examples/omilrec-v100-opt/task_workspace_local.yaml | New open-workspace OMILREC task. |

## Task 1: Replace the task schema

**Files:**

- Modify: simpleloop/config.py
- Modify: simpleloop/initialize.py
- Modify: tests/test_config_execution.py
- Modify: tests/test_initialize.py

**Consumes:** workspace.seed.path, workspace.seed.ref, workspace.copy, evaluation.runner, evaluation.args, evaluation.binds.

**Produces:** resolved workspace/evaluator fields described above.

- [ ] **Step 1: Write failing config tests**

~~~python
def test_workspace_task_resolves_seed_manifest_and_runner(tmp_path):
    cfg = config_mod.load(write_workspace_task(tmp_path))
    assert cfg["workspace_seed_ref"] == "baseline"
    assert cfg["workspace_copy"] == ["src", "CMakeLists.txt"]
    assert cfg["evaluator_args"] == ["--evtmax", "100"]

@pytest.mark.parametrize("legacy", ["source", "safety"])
def test_legacy_task_scope_blocks_are_rejected(tmp_path, legacy):
    raw = workspace_task(tmp_path)
    raw[legacy] = {}
    with pytest.raises(config_mod.ConfigError, match=legacy):
        config_mod.load(write_yaml(tmp_path, raw))
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: python -m pytest tests/test_config_execution.py -k 'workspace_task or legacy_task_scope' -v

Expected: FAIL because workspace/evaluation are unknown and source/safety remain valid.

- [ ] **Step 3: Implement strict schema helpers**

Add _resolve_workspace and _resolve_evaluation. Require a seed Git directory/ref, non-empty literal relative copy entries, an external regular-file runner, string args, and existing absolute evaluation binds. Reject absolute paths, parent traversal, empty entries, and .git in workspace.copy. Preserve existing metric and timeout validation.

- [ ] **Step 4: Change initialization semantics**

Make initialize verify seed path/ref read-only. Remove prepare_git behavior from task initialization; Workspace.setup will create the private package Git repository.

- [ ] **Step 5: Verify**

Run: python -m pytest tests/test_config_execution.py tests/test_initialize.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add simpleloop/config.py simpleloop/initialize.py tests/test_config_execution.py tests/test_initialize.py
git commit -m "feat: define mutable workspace task schema"
~~~

## Task 2: Materialize the private production package

**Files:**

- Modify: simpleloop/harness/workspace.py
- Create: tests/test_workspace_materialization.py
- Modify: tests/test_provenance_export_lock.py

**Consumes:** Workspace(run_dir, seed_path, seed_ref, copy_paths).

**Produces:** a private run_dir/repo containing only the manifest, committed as the package baseline; existing baseline_sha, add_worktree, remove_worktree, changed_paths, commit, and diff APIs stay available.

- [ ] **Step 1: Write failing materialization tests**

~~~python
def test_workspace_materializes_only_requested_seed_entries(tmp_path):
    seed, ref = make_seed_repo(tmp_path, {
        "src/main.cc": "int main() {}\\n",
        "CMakeLists.txt": "cmake_minimum_required(VERSION 3.20)\\n",
        "reference/golden.txt": "immutable\\n",
        "scripts/eval.sh": "#!/bin/sh\\n",
    })
    ws = Workspace(tmp_path / "run", seed, ref, ["src", "CMakeLists.txt"])
    ws.setup()
    assert (ws.repo / "src/main.cc").is_file()
    assert not (ws.repo / "reference").exists()
    assert not (ws.repo / "scripts").exists()
    assert (ws.repo / ".git").exists()

def test_worktree_can_delete_and_add_arbitrary_package_files(tmp_path):
    ws = materialized_workspace(tmp_path)
    wt = ws.add_worktree("candidate", ws.baseline_sha())
    (wt / "src/main.cc").unlink()
    (wt / "new_layout").mkdir()
    (wt / "new_layout/kernel.cc").write_text("int f() { return 1; }\\n")
    assert set(ws.changed_paths(wt)) == {"src/main.cc", "new_layout/kernel.cc"}
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: python -m pytest tests/test_workspace_materialization.py -v

Expected: FAIL because Workspace clones the complete source repository.

- [ ] **Step 3: Implement archive materialization**

Implement setup with these operations in order:

~~~python
self.repo.mkdir()
self._archive_seed_entries(destination=self.repo)
self._git(self.repo, "init")
self._git(self.repo, "add", "-A")
self._git(self.repo, "-c", "user.name=SimpleLoop",
          "-c", "user.email=loop@example.invalid",
          "commit", "-m", "SimpleLoop workspace baseline")
~~~

_archive_seed_entries must invoke git archive --format=tar seed_ref -- copy_paths without shell expansion, extract with tarfile, reject archive members escaping destination, and preserve executable file modes. Persist seed revision as provenance but use the internal baseline SHA for all later ancestry.

- [ ] **Step 4: Remove editable state**

Remove the editable constructor argument. changed_paths becomes factual provenance only.

- [ ] **Step 5: Verify**

Run: python -m pytest tests/test_workspace_materialization.py tests/test_provenance_export_lock.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add simpleloop/harness/workspace.py tests/test_workspace_materialization.py tests/test_provenance_export_lock.py
git commit -m "feat: materialize private mutable workspaces"
~~~

## Task 3: Isolate executor and evaluator mounts

**Files:**

- Modify: simpleloop/container/runtime.py
- Modify: simpleloop/roles/agent.py
- Modify: tests/test_runtime.py
- Modify: tests/test_agent_usage.py

**Produces:**

~~~python
def executor_exec_argv(self, payload: Sequence[str], workspace: Path) -> list[str]: ...
def evaluator_exec_argv(
    self, payload: Sequence[str], workspace: Path, runner: Path,
    evaluator_binds: Sequence[Path],
) -> list[str]: ...
~~~

- [ ] **Step 1: Write failing argv tests**

~~~python
def test_executor_argv_exposes_only_workspace(tmp_path):
    runtime = make_runtime(tmp_path, binds=[tmp_path / "secret"])
    work = tmp_path / "work"; work.mkdir()
    argv = runtime.executor_exec_argv(["claude", "-p"], work)
    assert f"{work.resolve()}:/work:rw" in argv
    assert all("secret" not in item for item in argv)
    assert argv[argv.index("--cwd") + 1] == "/work"

def test_evaluator_argv_mounts_runner_readonly(tmp_path):
    work = tmp_path / "work"; work.mkdir()
    runner = tmp_path / "eval" / "eval.sh"; runner.parent.mkdir()
    runner.write_text("#!/bin/sh\\n")
    argv = runtime.evaluator_exec_argv(
        ["bash", "/evaluator/eval.sh", "/work"], work, runner, [tmp_path])
    assert f"{runner.parent.resolve()}:/evaluator:ro" in argv
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: python -m pytest tests/test_runtime.py -k 'executor_argv or evaluator_argv' -v

Expected: FAIL with missing methods.

- [ ] **Step 3: Implement narrow capability builders**

Use containall, cleanenv, no-eval and current userns behavior. Executor gets /work:rw and no configured runtime.binds. Evaluator gets /work:rw, runner parent at /evaluator:ro, and every evaluator bind read-only. Validate runner is external to work and evaluator binds are directories.

- [ ] **Step 4: Route Agent**

Replace runtime.exec_argv(payload, cwd=cwd) in Agent._run with runtime.executor_exec_argv(payload, workspace=cwd). Host Popen cwd can remain the physical worktree.

- [ ] **Step 5: Verify**

Run: python -m pytest tests/test_runtime.py tests/test_agent_usage.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add simpleloop/container/runtime.py simpleloop/roles/agent.py tests/test_runtime.py tests/test_agent_usage.py
git commit -m "feat: isolate executor and evaluator mounts"
~~~

## Task 4: Invoke one external evaluator

**Files:**

- Modify: simpleloop/harness/evals.py
- Modify: simpleloop/execution/local.py
- Modify: simpleloop/candidate_worker.py
- Modify: tests/test_runtime.py
- Modify: tests/test_candidate_worker.py
- Modify: tests/test_parallel_candidates.py

**Produces:**

~~~python
def run_external_eval(
    runner: Path, args: list[str], workspace: Path, runtime: ApptainerRuntime,
    evaluator_binds: list[str], metrics_schema: dict | None = None,
    timeout_seconds: int = 600, output_cap: int = 16000,
) -> EvalResult: ...
~~~

- [ ] **Step 1: Write failing evaluator contract tests**

~~~python
def test_external_eval_passes_work_as_first_runner_argument(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(evals_mod.subprocess, "run", capture_run(seen))
    result = evals_mod.run_external_eval(
        tmp_path / "eval" / "eval.sh", ["--evtmax", "100"],
        tmp_path / "work", FakeRuntime(), [], _SCHEMA)
    assert seen["argv"][-5:] == [
        "bash", "/evaluator/eval.sh", "/work", "--evtmax", "100"]
    assert result.metrics["SPEED_MS"] == 12.5
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: python -m pytest tests/test_runtime.py tests/test_candidate_worker.py -k external_eval -v

Expected: FAIL because command-list evaluation is still used.

- [ ] **Step 3: Implement the runner call**

Build payload without shell interpolation:

~~~python
payload = ["bash", f"/evaluator/{runner.name}", "/work", *args]
argv = runtime.evaluator_exec_argv(
    payload, workspace=workspace, runner=runner,
    evaluator_binds=evaluator_binds,
)
~~~

Preserve EvalResult and metric parsing. Update local baseline, worker baseline, and candidate evaluation to call this API.

- [ ] **Step 4: Remove the allowlist acceptance decision**

Do not call check_diff. Keep changed_paths for history. Set PATHS passed because physical mount isolation guarantees candidate edits occur inside work. Remove PATH_GATE_REJECTED status and editable/frozen executor parameters.

- [ ] **Step 5: Verify**

Run: python -m pytest tests/test_runtime.py tests/test_candidate_worker.py tests/test_parallel_candidates.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add simpleloop/harness/evals.py simpleloop/execution/local.py simpleloop/candidate_worker.py tests/test_runtime.py tests/test_candidate_worker.py tests/test_parallel_candidates.py
git commit -m "feat: evaluate mutable workspaces externally"
~~~

## Task 5: Present only the open workspace to agents

**Files:**

- Modify: simpleloop/roles/executor.py
- Modify: simpleloop/memory/context.py
- Modify: simpleloop/roles/proposer.py
- Modify: simpleloop/roles/research_agent.py
- Modify: simpleloop/roles/research_tools.py
- Modify: simpleloop/prompts/proposer.md
- Modify: tests/test_prompt_templates.py
- Modify: tests/test_scientist_proposer.py
- Modify: tests/test_research_tools.py
- Modify: tests/test_proposer_harness.py

**Produces:** proposer context consuming goal and gate_block only; executor context consuming goal, gate_block, proposal, and the open-workspace statement.

- [ ] **Step 1: Write failing context tests**

~~~python
def test_fresh_proposer_context_has_no_seed_or_allowlist():
    text = build_fresh_inquiry_context(goal="g", gate_block="FCN: exact")
    assert "Editable paths" not in text
    assert "Frozen paths" not in text
    assert "source" not in text.lower()
    assert "/work" in text
    assert "all task-specific prior knowledge" in text

def test_executor_prompt_grants_full_workspace_ownership():
    prompt = captured_executor_prompt(...)
    assert "create, delete, move, replace, or reorganize" in prompt
    assert "Editable paths" not in prompt
    assert "Frozen paths" not in prompt
    assert "evaluator" not in prompt.lower()
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: python -m pytest tests/test_prompt_templates.py tests/test_scientist_proposer.py tests/test_research_tools.py -k 'workspace or executor_prompt' -v

Expected: FAIL because current contexts contain source/editable/frozen data.

- [ ] **Step 3: Apply the approved text**

Render the Workspace and Evaluation contract from this plan verbatim. Do not include evaluator runner, eval command, seed path, baseline SHA, copy manifest, asset names, or path constraints in either role prompt.

- [ ] **Step 4: Rename research boundary**

Rename researcher-visible source mount and wording to workspace and /work. Update Git worktree environment to GIT_WORK_TREE=/work. New evidence refs use workspace:path; accept source:path only for legacy history readers. Do not change the cognitive phase protocol.

- [ ] **Step 5: Verify**

Run: python -m pytest tests/test_prompt_templates.py tests/test_scientist_proposer.py tests/test_research_tools.py tests/test_proposer_harness.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add simpleloop/roles/executor.py simpleloop/memory/context.py simpleloop/roles/proposer.py simpleloop/roles/research_agent.py simpleloop/roles/research_tools.py simpleloop/prompts/proposer.md tests/test_prompt_templates.py tests/test_scientist_proposer.py tests/test_research_tools.py tests/test_proposer_harness.py
git commit -m "feat: present agents with an open mutable workspace"
~~~

## Task 6: Thread the contract through normal and standalone runs

**Files:**

- Modify: simpleloop/loop.py
- Modify: simpleloop/candidate_worker.py
- Modify: scripts/proposer_harness.py
- Modify: simpleloop/cli.py
- Modify: tests/test_proposer_harness_runner.py
- Modify: tests/test_proposer_cli.py
- Modify: tests/test_static_mode.py
- Modify: tests/test_telemetry.py

- [ ] **Step 1: Write failing pipeline tests**

~~~python
def test_standalone_proposer_uses_materialized_workspace(monkeypatch, tmp_path):
    calls = install_workspace_fakes(monkeypatch, tmp_path)
    harness.run_proposer(tmp_path / "task.yaml", tmp_path / "out")
    assert calls["workspace"]["copy"] == ["src", "CMakeLists.txt"]
    assert "editable" not in calls["orchestrator_run"]
    assert "frozen" not in calls["orchestrator_run"]
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: python -m pytest tests/test_proposer_harness_runner.py tests/test_proposer_cli.py tests/test_static_mode.py tests/test_telemetry.py -k workspace -v

Expected: FAIL because source/safety fields are threaded through the pipeline.

- [ ] **Step 3: Update construction and provenance**

Construct Workspace from resolved workspace fields everywhere. Keep private baseline SHA in history/result metadata. For standalone from-run, use that run private package repo, not the original seed. Update CLI/resolved snapshot wording to workspace seed, copy entries, and external evaluator runner; none of this is passed to agents.

- [ ] **Step 4: Verify**

Run: python -m pytest tests/test_proposer_harness_runner.py tests/test_proposer_cli.py tests/test_static_mode.py tests/test_telemetry.py tests/test_parallel_candidates.py -v

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add simpleloop/loop.py simpleloop/candidate_worker.py scripts/proposer_harness.py simpleloop/cli.py tests/test_proposer_harness_runner.py tests/test_proposer_cli.py tests/test_static_mode.py tests/test_telemetry.py tests/test_parallel_candidates.py
git commit -m "feat: run proposer and candidates from work packages"
~~~

## Task 7: Add the OMILREC external evaluator example

**Files:**

- Create: examples/omilrec-v100-opt/evaluator/eval.sh
- Create: examples/omilrec-v100-opt/task_workspace_local.yaml
- Modify: examples/omilrec-v100-opt/task_hints_local.yaml
- Modify: tests/test_example_apptainer.py
- Modify: tests/test_config_execution.py

- [ ] **Step 1: Write failing example tests**

~~~python
def test_omilrec_workspace_example_has_no_source_or_safety():
    raw = yaml.safe_load(EXAMPLE.read_text())
    assert "workspace" in raw and "evaluation" in raw
    assert "source" not in raw and "safety" not in raw

def test_omilrec_runner_requires_workspace_argument():
    text = RUNNER.read_text()
    assert 'workspace="$1"' in text
    assert 'test -d "$workspace"' in text
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: python -m pytest tests/test_example_apptainer.py tests/test_config_execution.py -k omilrec_workspace -v

Expected: FAIL because the task and external runner do not exist.

- [ ] **Step 3: Implement immutable adapter and manifest**

The runner starts with set -euo pipefail, validates argv[1] as /work, and uses its own immutable directory for current evaluator scripts, references, benchmarks, and gate probes. It uses only the supplied workspace as the production/build root and emits unchanged metric names. The task manifest starts with OMILRECV2, required root/package CMake files, and production-only build inputs. Do not add tests, baseline, benchmark, reference, or evaluator directories to the package.

- [ ] **Step 4: Verify baseline**

Run:

~~~bash
python -m pytest tests/test_example_apptainer.py tests/test_config_execution.py -v
python -m simpleloop run examples/omilrec-v100-opt/task_workspace_local.yaml --rounds 0
~~~

Expected: tests PASS and baseline evaluator reports SPEED_MS plus every configured gate.

- [ ] **Step 5: Commit**

~~~bash
git add examples/omilrec-v100-opt/evaluator/eval.sh examples/omilrec-v100-opt/task_workspace_local.yaml examples/omilrec-v100-opt/task_hints_local.yaml tests/test_example_apptainer.py tests/test_config_execution.py
git commit -m "feat: add OMILREC external workspace evaluator"
~~~

## Task 8: Full verification and capability audit

**Files:** modify only files from Tasks 1-7 if a real failure exposes a defect.

- [ ] **Step 1: Run focused integration tests**

Run: python -m pytest tests/test_workspace_materialization.py tests/test_runtime.py tests/test_candidate_worker.py tests/test_proposer_harness_runner.py tests/test_scientist_proposer.py -v

Expected: PASS.

- [ ] **Step 2: Run complete suite**

Run: python -m pytest

Expected: PASS.

- [ ] **Step 3: Run project checks and diff audit**

Run:

~~~bash
npm test
npm run typecheck
git diff --check
git status --short
~~~

Expected: configured project checks pass, diff check has no output, and status contains intended files only.

- [ ] **Step 4: Manually audit capabilities**

Confirm captured argv and prompts prove:

~~~text
proposer: /work:ro + /scratch:rw, no evaluator mount or seed metadata
executor: /work:rw, no evaluator mount and no configured broad binds
evaluator: /work + /evaluator:ro + evaluator-only read-only binds
~~~

Confirm a candidate can add a new source file and modify CMake without a path gate. Confirm a baseline/reference file is absent from the materialized package.

- [ ] **Step 5: Commit audit fixes only if required**

~~~bash
git add -A
git commit -m "test: verify open workspace evaluation boundary"
~~~

## Plan self-review

- Coverage: Tasks 1-2 remove source/safety and build the private package. Tasks 3-4 establish capability-separated containers and eval.sh /work. Task 5 removes task-specific implementation/evaluator details from agent context. Task 6 updates normal and standalone execution. Task 7 supplies OMILREC migration. Task 8 verifies both behavior and isolation.
- No placeholders: the OMILREC copy manifest is intentionally baseline-driven in Task 7; the acceptance rule is explicit and forbids evaluator assets.
- Consistency: Workspace retains its baseline/worktree/commit responsibilities while changing only initial materialization. Every evaluation path uses the same external runner interface.
