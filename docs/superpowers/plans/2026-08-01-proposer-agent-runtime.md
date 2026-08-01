# Proposer Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-shot Claude Proposer with a bounded HEPAI-powered scientific agent that can investigate code and factual experiment memory before submitting exactly K proposals, while preserving the existing Proposer → Executor → Harness outer loop.

**Architecture:** The Proposer owns an internal JSON-action loop and four research actions plus one terminal action. Read-only research commands run in an isolated Apptainer sandbox; `history.jsonl` remains Harness-owned factual memory, while bounded Proposer Insights live in a separate append-only `insights.jsonl`. The Executor remains the existing Claude Code agent, and the Harness remains the only authority for evaluation, gates, and parent selection.

**Tech Stack:** Python 3.9+, HEPAI Chat Completions (`hepai>=1.1.13`), Apptainer, Git worktrees, pytest, YAML.

## Global Constraints

- Keep the macro loop unchanged: Proposer → Executor → Harness eval/gates.
- Do not add Judger, provider registry, retry/repair loop, native function calling, persistent scratch, web access, or Proposer-to-Worker calls.
- Do not add compatibility branches for old Insights or old dynamic runs. `researcher` is required for normal agent-driven execution and ignored by static proposal mode.
- The semantic prompt describes scientific identity and epistemic principles; Python enforces actions, permissions, budgets, and output shape.
- HEPAI credentials come only from `HEPAI_API_KEY`; never serialize or forward them into the research container or batch jobs.
- Every implementation step starts with a failing test and ends with the smallest code that passes it.
- Preserve the existing Executor and authoritative Harness behavior.

---

## Task 1: Add the narrow model boundary and strict researcher config

**Necessity:** The Proposer needs one model transport now, while a three-method boundary prevents HEPAI details from leaking into research logic and is the minimum needed for later provider replacement. Strict config is necessary to make budgets and the selected endpoint auditable. No provider registry or fallback is added.

**Files:**

- Create: `simpleloop/roles/model.py`
- Modify: `simpleloop/config.py:1-220`
- Modify: `pyproject.toml:1-15`
- Create: `tests/test_model.py`
- Modify: `tests/test_config_execution.py`

- [ ] **Step 1: Write failing model adapter tests**

```python
# tests/test_model.py
from types import SimpleNamespace

import pytest

from simpleloop.roles.model import HepAIChatModel, ModelError


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        message = SimpleNamespace(content='{"action":"submit_proposals"}')
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 17})
        return SimpleNamespace(choices=[choice], usage=usage)


def test_hepai_uses_nonstreaming_chat_completion_and_timeout():
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = HepAIChatModel(client=client, model="gpt-5.5")

    reply = model.complete(
        system="scientist",
        messages=[{"role": "user", "content": "investigate"}],
        timeout_seconds=12.5,
    )

    assert completions.kwargs == {
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": "scientist"},
            {"role": "user", "content": "investigate"},
        ],
        "stream": False,
        "timeout": 12.5,
    }
    assert reply.text == '{"action":"submit_proposals"}'
    assert reply.usage == {"total_tokens": 17}


def test_hepai_requires_key_when_constructing_real_client(monkeypatch):
    monkeypatch.delenv("HEPAI_API_KEY", raising=False)
    with pytest.raises(ModelError, match="HEPAI_API_KEY"):
        HepAIChatModel.from_config({
            "model": "gpt-5.5",
            "base_url": "https://aiapi.ihep.ac.cn/apiv2",
        })
```

- [ ] **Step 2: Run the focused tests and confirm the import fails**

Run: `python -m pytest -q tests/test_model.py`

Expected: FAIL because `simpleloop.roles.model` does not exist.

- [ ] **Step 3: Implement only the protocol and HEPAI adapter**

```python
# simpleloop/roles/model.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelReply:
    text: str
    usage: object = None


class ChatModel(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        timeout_seconds: float,
    ) -> ModelReply: ...


class HepAIChatModel:
    def __init__(self, *, client, model: str):
        self.client = client
        self.model = model

    @classmethod
    def from_config(cls, config: dict) -> "HepAIChatModel":
        key = os.environ.get("HEPAI_API_KEY")
        if not key:
            raise ModelError("HEPAI_API_KEY is required for the HEPAI proposer")
        try:
            from hepai import HepAI
        except ImportError as exc:
            raise ModelError("install the project dependency 'hepai'") from exc
        return cls(
            client=HepAI(api_key=key, base_url=config["base_url"]),
            model=config["model"],
        )

    def complete(self, *, system, messages, timeout_seconds) -> ModelReply:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            stream=False,
            timeout=timeout_seconds,
        )
        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise ModelError("HEPAI returned an empty assistant message")
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return ModelReply(text=text, usage=usage)
```

Do not add provider selection classes, capability discovery, temperature, retry, or fallback.

- [ ] **Step 4: Add failing strict-config tests**

Add tests to `tests/test_config_execution.py` using its existing
`_base_task(tmp_path)` helper:

```python
def test_researcher_defaults(tmp_path):
    raw = _base_task(tmp_path)
    raw["researcher"] = {}
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = config_mod.load(path)
    assert cfg["researcher"] == {
        "api": "hepai",
        "model": "gpt-5.5",
        "base_url": "https://aiapi.ihep.ac.cn/apiv2",
        "max_steps": 20,
        "command_timeout_seconds": 120,
        "command_output_cap_chars": 12000,
    }


def test_researcher_rejects_unknown_key(tmp_path):
    raw = _base_task(tmp_path)
    raw["researcher"] = {"temperature": 0.2}
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="researcher.*temperature"):
        config_mod.load(path)
```

Also parameterize invalid values: `api != "hepai"`, blank model/base URL, `max_steps < 1`, command timeout `< 1`, and output cap `< 1000`.

- [ ] **Step 5: Resolve the optional strict block**

In `simpleloop/config.py`, add `researcher` to `TASK_TOP_KEYS` and a single resolver:

```python
_RESEARCHER_DEFAULTS = {
    "api": "hepai",
    "model": "gpt-5.5",
    "base_url": "https://aiapi.ihep.ac.cn/apiv2",
    "max_steps": 20,
    "command_timeout_seconds": 120,
    "command_output_cap_chars": 12000,
}


def _resolve_researcher(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("researcher: must be an object")
    unknown = set(raw) - set(_RESEARCHER_DEFAULTS)
    if unknown:
        raise ConfigError(f"researcher: unknown key(s): {sorted(unknown)}")
    result = {**_RESEARCHER_DEFAULTS, **raw}
    if result["api"] != "hepai":
        raise ConfigError("researcher.api: first version supports only 'hepai'")
    for key in ("model", "base_url"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ConfigError(f"researcher.{key}: must be a non-empty string")
    for key, minimum in (
        ("max_steps", 1),
        ("command_timeout_seconds", 1),
        ("command_output_cap_chars", 1000),
    ):
        if not isinstance(result[key], int) or result[key] < minimum:
            raise ConfigError(f"researcher.{key}: must be an integer >= {minimum}")
    return result
```

Resolve it with
`_resolve_researcher(raw["researcher"]) if "researcher" in raw else None`
and return that value as `"researcher"`. Missing block remains valid at
config-load time because static proposal mode does not need a model; an
explicit YAML `null` is rejected as a non-object. Task 5 adds the normal-mode
preflight failure.

- [ ] **Step 6: Declare the sole new runtime dependency and verify**

Change:

```toml
dependencies = ["pyyaml>=5.1", "matplotlib>=3.5", "hepai>=1.1.13"]
```

Run: `python -m pytest -q tests/test_model.py tests/test_config_execution.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml simpleloop/config.py simpleloop/roles/model.py tests/test_model.py tests/test_config_execution.py
git commit -m "feat: add HEPAI proposer model boundary"
```

---

## Task 2: Add factual lookup and bounded Insight memory

**Necessity:** Recent factual history alone is insufficient for long runs, while injecting all raw history is unbounded. Search plus single-episode inspection gives the Proposer on-demand access to the full factual record. One short Insight per completed round is the minimum semantic index; it is explicitly non-authoritative and separate from Harness facts.

**Files:**

- Modify: `simpleloop/harness/memory.py:1-90`
- Create: `simpleloop/roles/research_tools.py`
- Modify: `tests/test_memory.py`
- Create: `tests/test_research_tools.py`

- [ ] **Step 1: Write failing full-history search and inspection tests**

In `tests/test_memory.py`, require `resolve_episode()` to include the already bounded `eval_block` stored by `Store`:

```python
assert resolved["eval_block"] == "objective=4.2\nphysics_gate=1"
```

In `tests/test_research_tools.py`:

```python
from simpleloop.roles.research_tools import search_history


def test_search_history_matches_facts_newest_first(history):
    matches = search_history(history, "physics_gate", limit=20)
    assert [item["ref"] for item in matches] == ["r2c0", "r0c0"]
    assert all("eval_block" not in item for item in matches)


def test_search_history_requires_query(history):
    with pytest.raises(ValueError, match="non-empty"):
        search_history(history, "   ")
```

The fixture should include proposal text, status, changed paths, metric keys, and gate keys so each field is covered. Assert the hard cap of 20.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `python -m pytest -q tests/test_memory.py tests/test_research_tools.py`

Expected: FAIL because search and `eval_block` exposure are absent.

- [ ] **Step 3: Implement deterministic factual lookup**

Change `resolve_episode()` to return `"eval_block": candidate.get("eval_block") or ""`; do not add an old-schema fallback.

Add to `research_tools.py`:

```python
def search_history(history: list[dict], query: str, *, limit: int = 20) -> list[dict]:
    needle = str(query).strip().casefold()
    if not needle:
        raise ValueError("history query must be non-empty")
    matches = []
    for record in reversed(history):
        for candidate in reversed(record.get("candidates") or []):
            ref = f"r{record['round']}c{candidate['candidate']}"
            haystack = json.dumps({
                "ref": ref,
                "proposal": candidate.get("proposal"),
                "status": candidate.get("status"),
                "changed_paths": candidate.get("changed_paths") or [],
                "metric_keys": list((candidate.get("metrics") or {}).keys()),
                "gate_keys": list((candidate.get("gates") or {}).keys()),
            }, ensure_ascii=False).casefold()
            if needle in haystack:
                episode = resolve_episode(history, ref)
                matches.append({
                    key: value for key, value in episode.items()
                    if key != "eval_block"
                })
                if len(matches) == limit:
                    return matches
    return matches
```

- [ ] **Step 4: Write failing Insight persistence tests**

```python
from simpleloop.roles.research_tools import Insight, InsightStore


def test_insight_store_is_append_only_and_idempotent(tmp_path):
    store = InsightStore(tmp_path / "insights.jsonl")
    insight = Insight(text="Cache misses dominate.", refs=("r0c0",))
    assert store.append(round_id=0, insight=insight) is True
    assert store.append(round_id=0, insight=insight) is False
    assert store.load() == [{
        "id": "I0", "round": 0, "text": "Cache misses dominate.",
        "refs": ["r0c0"],
    }]


def test_insight_store_rejects_conflict(tmp_path):
    store = InsightStore(tmp_path / "insights.jsonl")
    store.append(0, Insight("A", ("r0c0",)))
    with pytest.raises(ValueError, match="conflicting"):
        store.append(0, Insight("B", ("r0c0",)))
```

Also test: text is nonblank and at most 500 characters; at least one ref is required; every ref must resolve against current history before it becomes pending.

- [ ] **Step 5: Implement the minimal Insight types and store**

```python
@dataclass(frozen=True)
class Insight:
    text: str
    refs: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"text": self.text, "refs": list(self.refs)}

    @classmethod
    def from_dict(cls, value: dict) -> "Insight":
        if set(value) != {"text", "refs"}:
            raise ValueError("insight must contain only text and refs")
        text = value["text"]
        refs = value["refs"]
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("insight text must be 1..500 characters")
        if not isinstance(refs, list) or not refs or not all(
            isinstance(ref, str) for ref in refs
        ):
            raise ValueError("insight refs must be a non-empty string list")
        return cls(text.strip(), tuple(refs))


class InsightStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(
            encoding="utf-8").splitlines() if line.strip()]

    def append(self, round_id: int, insight: Insight) -> bool:
        record = {"id": f"I{round_id}", "round": round_id, **insight.to_dict()}
        existing = next((row for row in self.load() if row["id"] == record["id"]), None)
        if existing == record:
            return False
        if existing is not None:
            raise ValueError(f"conflicting insight {record['id']}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
```

`InsightStore.load()` must validate the exact current schema rather than silently accepting old records. Add `render_insights(records)` that serializes all records for the prompt without interpreting or ranking them.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest -q tests/test_memory.py tests/test_research_tools.py`

Expected: PASS.

```bash
git add simpleloop/harness/memory.py simpleloop/roles/research_tools.py tests/test_memory.py tests/test_research_tools.py
git commit -m "feat: add proposer research memory"
```

---

## Task 3: Add the read-only research-command sandbox

**Necessity:** Code reading, Git comparison, and local analysis require a general research tool. Raw host Bash would expose credentials and mutable project state, so the only acceptable MVP is one sandboxed Bash action with read-only source/history mounts, ephemeral scratch, no network, a timeout, and an output cap. No command allowlist or extra file tools are needed because Bash subsumes them inside the boundary.

**Files:**

- Modify: `simpleloop/container/runtime.py:1-190`
- Extend: `simpleloop/roles/research_tools.py`
- Modify: `tests/test_runtime.py`
- Extend: `tests/test_research_tools.py`

- [ ] **Step 1: Write failing sandbox argv and environment tests**

```python
def test_research_argv_is_contained_read_only_and_offline(runtime, tmp_path):
    argv = runtime.research_exec_argv(
        ["bash", "-lc", "git show --stat HEAD"],
        source=tmp_path / "source",
        repo=tmp_path / "repo",
        history=tmp_path / "run",
        scratch=tmp_path / "scratch",
        cwd="source",
    )
    assert "--containall" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert f"{tmp_path / 'source'}:/source:ro" in argv
    assert f"{tmp_path / 'repo'}:/repo:ro" in argv
    assert f"{tmp_path / 'run'}:/history:ro" in argv
    assert f"{tmp_path / 'scratch'}:/scratch:rw" in argv
    assert argv[argv.index("--cwd") + 1] == "/source"


def test_research_env_contains_no_credentials(runtime, monkeypatch):
    monkeypatch.setenv("HEPAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CONDOR_TOKEN", "secret")
    env = runtime.research_subprocess_env()
    assert not any("KEY" in key or "TOKEN" in key for key in env)
```

- [ ] **Step 2: Run and confirm missing methods**

Run: `python -m pytest -q tests/test_runtime.py -k research`

Expected: FAIL because the research-specific runtime methods do not exist.

- [ ] **Step 3: Implement a separate hardened launcher path**

Add methods without changing the existing Executor/eval `exec_argv()` behavior:

```python
def research_exec_argv(
    self, payload, *, source, repo, history, scratch, cwd
) -> list[str]:
    if cwd not in {"source", "scratch"}:
        raise ValueError("research cwd must be 'source' or 'scratch'")
    argv = [
        self.executable, "exec", "--cleanenv", "--no-eval",
        "--containall", "--net", "--network", "none",
    ]
    if os.environ.get("SIMPLELOOP_APPTAINER_USERNS", "1") != "0":
        argv.append("--userns")
    for bind in self.binds:
        argv.extend(["--bind", f"{bind}:{bind}:ro"])
    argv.extend([
        "--bind", f"{Path(source).resolve()}:/source:ro",
        "--bind", f"{Path(repo).resolve()}:/repo:ro",
        "--bind", f"{Path(history).resolve()}:/history:ro",
        "--bind", f"{Path(scratch).resolve()}:/scratch:rw",
        "--cwd", f"/{cwd}", str(self.image),
    ])
    return [*argv, *map(str, payload)]


def research_subprocess_env(self) -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "LD_LIBRARY_PATH"}
    return {key: value for key, value in os.environ.items() if key in allowed}
```

The research payload will receive `GIT_DIR=/repo/.git` and `GIT_WORK_TREE=/source` through the explicit `env` program arguments, not the host environment.

- [ ] **Step 4: Write failing command-runner tests**

Use a fake `subprocess.Popen` to assert:

- `start_new_session=True` is set;
- `cwd="source"` and `cwd="scratch"` dispatch to the correct virtual cwd;
- non-zero exit is returned as an observation, not raised;
- timeout calls `os.killpg` and returns `timed_out=True`;
- combined stdout/stderr is deterministically truncated to the configured total cap;
- a blank command and unknown cwd are rejected before spawning.

Expected observation shape:

```python
{
    "ok": True,
    "returncode": 0,
    "timed_out": False,
    "truncated": False,
    "output": "git output",
}
```

- [ ] **Step 5: Implement `ResearchCommandRunner` in `research_tools.py`**

The runner must invoke:

```python
payload = [
    "env",
    "GIT_DIR=/repo/.git",
    "GIT_WORK_TREE=/source",
    "bash", "-lc", command,
]
```

Use `subprocess.Popen(..., shell=False, text=True, stdout=PIPE, stderr=PIPE, start_new_session=True)`. On `TimeoutExpired`, kill the process group with `os.killpg(process.pid, signal.SIGKILL)` and collect remaining output. Combine stdout and stderr once, retain the first `output_cap_chars`, and set `truncated`. A non-zero command is a valid tool observation with `ok=False`; only malformed action input is a contract error.

The Proposer call creates its scratch with `tempfile.TemporaryDirectory(prefix="simpleloop-research-")`, and its caller removes the read-only Git worktree in `finally`. Do not persist shell state or scratch.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest -q tests/test_runtime.py tests/test_research_tools.py`

Expected: PASS.

```bash
git add simpleloop/container/runtime.py simpleloop/roles/research_tools.py tests/test_runtime.py tests/test_research_tools.py
git commit -m "feat: sandbox proposer research commands"
```

---

## Task 4: Replace the one-shot Proposer with the bounded scientific action loop

**Necessity:** A one-shot completion cannot investigate, update hypotheses, or retrieve evidence adaptively. A bounded ReAct-like loop is the minimum agent runtime. The workflow order is deliberately not encoded; only action schemas, authority boundaries, and budgets are fixed.

**Files:**

- Rewrite: `simpleloop/roles/proposer.py`
- Extend: `simpleloop/roles/research_tools.py`
- Modify: `simpleloop/prompts/proposer.md`
- Create: `tests/test_proposer_agent.py`
- Modify: `tests/test_prompt_templates.py`

- [ ] **Step 1: Write failing action-contract tests**

Cover exactly these JSON objects and reject extra keys:

```json
{"action":"run_research_command","command":"rg cache src","cwd":"source"}
{"action":"search_history","query":"cache"}
{"action":"inspect_episode","ref":"r2c0"}
{"action":"write_insight","text":"Cache misses dominate.","refs":["r2c0"]}
{"action":"submit_proposals","proposals":["Replace the cache layout with a contiguous array and preserve the public API."]}
```

Tests must assert malformed JSON, unknown actions, wrong keys, blank queries/commands, invalid cwd, invalid refs, and proposal count mismatches fail without a repair completion.

- [ ] **Step 2: Write failing loop-behavior tests using fakes**

Use a scripted `FakeModel` and fake tools. Assert:

- multiple research actions can occur in any order before submission;
- each observation is appended to the next model call;
- `write_insight` replaces the prior pending Insight in the same call;
- an invalid Insight ref returns an operational error observation and is not pending;
- exactly K nonblank proposals are terminal;
- reaching `max_steps` raises `ProposerError`;
- the remaining outer deadline is passed to every model call and command;
- usage from every completion is sent to `usage_observer`;
- the Proposer cannot invoke Executor or Harness because neither object is present in its constructor or tools.

- [ ] **Step 3: Implement strict action parsing and tool dispatch**

`ResearchTools.execute(action)` handles the four non-terminal actions. `write_insight` validates each ref by calling `resolve_episode()` and replaces `self.pending_insight`. `inspect_episode` returns the complete bounded episode. `submit_proposals` stays in the Proposer because it is the terminal runtime contract, not an external tool.

Use this result type:

```python
@dataclass(frozen=True)
class ProposerResult:
    proposals: list[str]
    insight: Insight | None
    usage: object = None
```

- [ ] **Step 4: Implement the bounded Proposer agent**

The public interface is exactly:

```text
ProposerAgent(
    *, model: ChatModel, runtime: ApptainerRuntime,
    timeout_seconds: int, max_steps: int,
    command_timeout_seconds: int, command_output_cap_chars: int,
    usage_observer=None,
)

ProposerAgent.run(
    *, goal: str, editable: list[str], frozen: list[str],
    history: list[dict], insights: list[dict], base_sha: str,
    source_path: Path, repo_path: Path, run_dir: Path,
    candidates_per_round: int, recent_rounds: int,
    gate_block: str, prompt_dir: Path | None,
) -> ProposerResult
```

The message loop is:

```python
deadline = time.monotonic() + self.timeout_seconds
messages = [{"role": "user", "content": initial_context}]
with TemporaryDirectory(prefix="simpleloop-research-") as scratch:
    tools = ResearchTools(
        runtime=self.runtime,
        source=source_path,
        repo=repo_path,
        history_dir=run_dir,
        scratch=Path(scratch),
        history=history,
        command_timeout_seconds=self.command_timeout_seconds,
        command_output_cap_chars=self.command_output_cap_chars,
    )
    usages = []
    for _ in range(self.max_steps):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProposerError("proposer deadline exceeded")
        reply = self.model.complete(
            system=system_prompt,
            messages=messages,
            timeout_seconds=remaining,
        )
        usages.append(reply.usage)
        if self.usage_observer and reply.usage:
            self.usage_observer(reply.usage)
        action = parse_action(reply.text, candidates_per_round)
        if action["action"] == "submit_proposals":
            return ProposerResult(
                proposals=action["proposals"],
                insight=tools.pending_insight,
                usage=usages,
            )
        observation = tools.execute(action, deadline=deadline)
        messages.extend([
            {"role": "assistant", "content": reply.text},
            {"role": "user", "content": json.dumps(
                {"tool_result": observation}, ensure_ascii=False)},
        ])
raise ProposerError("proposer exceeded researcher.max_steps")
```

The initial context contains the task goal, gates, current accepted SHA, editable/frozen paths, only `views.for_proposer(history, recent_rounds=...)`, all compact Insight records, and the exact requested proposal count. It does not contain all raw history.

- [ ] **Step 5: Separate semantic identity from immutable runtime protocol**

Rewrite `simpleloop/prompts/proposer.md` around these principles, without a fixed checklist:

```markdown
You are the lead scientific researcher optimizing the user's stated objective.
Choose what to investigate and how deeply to investigate from the available
evidence. Distinguish Harness facts, direct observations, hypotheses,
inferences, and uncertainty. Treat failed experiments as evidence. Revise or
abandon a direction when evidence warrants it. Prefer informative executable
experiments over ritual compliance. Only Harness outputs establish evaluation
and gate facts.
```

Append the exact JSON action schemas, path meanings, budget/authority boundaries, and “one JSON object only” rule from an immutable Python string. Tests must prove that editing the semantic prompt cannot grant Worker/Harness access or change the action schema.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest -q tests/test_proposer_agent.py tests/test_prompt_templates.py tests/test_research_tools.py`

Expected: PASS.

```bash
git add simpleloop/roles/proposer.py simpleloop/roles/research_tools.py simpleloop/prompts/proposer.md tests/test_proposer_agent.py tests/test_prompt_templates.py
git commit -m "feat: make proposer a scientific agent"
```

---

## Task 5: Integrate the agent without changing the outer loop

**Necessity:** Integration must preserve the existing authority graph and resume behavior. Carrying one pending Insight in the inflight metadata is the minimum state needed to avoid re-proposing after an HEPJob interruption. Moving journal deletion to the outer business commit is necessary; a new transaction framework is not.

**Files:**

- Modify: `simpleloop/loop.py:68-475`
- Modify: `simpleloop/execution/hepjob.py:528-572`
- Modify: `tests/test_static_mode.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_hepjob_backend.py`
- Modify: `tests/test_provenance_export_lock.py`

- [ ] **Step 1: Write failing static/dynamic construction tests**

Assert:

- normal mode without `researcher` fails before baseline evaluation;
- normal mode without `HEPAI_API_KEY` fails before baseline evaluation;
- static `--proposals` mode does not construct `HepAIChatModel`, does not require a key or researcher block, and writes no Insight;
- dynamic mode constructs `ProposerAgent`, while Executor still uses the existing Claude `Agent` with `Read,Edit,Write,Bash`.

- [ ] **Step 2: Load static proposals before building context**

Change `_run_locked()` ordering to:

```python
static_proposals = _load_proposals(proposals)
if static_proposals is None and cfg.get("researcher") is None:
    raise config_mod.ConfigError("researcher: required for agent-driven runs")
ctx = _build_context(
    cfg, run_dir_path, resume=continue_run, prompt_dir=prompt_path,
    enable_researcher=static_proposals is None,
)
```

This is one mode decision, not a legacy-run compatibility branch.

- [ ] **Step 3: Replace only the Proposer construction in `RunContext`**

Add `insight_store: InsightStore` and type `proposer_agent` as `ProposerAgent | None`. In `_build_context()`:

```python
proposer_agent = None
if enable_researcher:
    research_cfg = cfg["researcher"]
    proposer_agent = ProposerAgent(
        model=HepAIChatModel.from_config(research_cfg),
        runtime=runtime,
        timeout_seconds=cfg["agent_timeout_seconds"],
        max_steps=research_cfg["max_steps"],
        command_timeout_seconds=research_cfg["command_timeout_seconds"],
        command_output_cap_chars=research_cfg["command_output_cap_chars"],
        usage_observer=telemetry.record_usage,
    )
executor_agent = Agent(
    runtime=runtime, command="claude", timeout_seconds=timeout,
    allowed_tools="Read,Edit,Write,Bash",
    max_output_tokens=max_output_tokens,
    usage_observer=telemetry.record_usage,
)
```

Do not generalize `simpleloop/roles/agent.py`; it remains the Executor's Claude Code adapter.

- [ ] **Step 4: Write failing proposal-worktree and memory tests**

Assert `_next_proposals()`:

- creates an isolated research worktree at `parent_sha` using the existing
  `Workspace` lifecycle;
- passes that snapshot read-only to `ProposerAgent.run()`;
- reads recent facts from `history.jsonl` and all Insights from `insights.jsonl`;
- removes the worktree in `finally`, including model/tool failure;
- returns `ProposerResult`, with static mode returning `ProposerResult([text], None)`.

Use the existing `Workspace.add_worktree()` / `remove_worktree()` with a reserved `proposer-r<round>` id. Do not create another source-copy abstraction.

- [ ] **Step 5: Write failing inflight Insight lifecycle tests**

For a dynamic HEPJob round, assert the journal meta is exactly:

```json
{
  "round_id": 3,
  "parent_sha": "0123456789abcdef",
  "proposals": ["Replace the cache layout with a contiguous array."],
  "insight": {"text": "Cache misses dominate.", "refs": ["r2c0"]}
}
```

Also assert:

- resume reconstructs the pending Insight and skips the Proposer;
- a terminal round appends `history.jsonl`, then at most one `I<round>` Insight, then clears the journal;
- replaying the same round does not duplicate the Insight;
- if history was appended but the process died before Insight/journal cleanup,
  `--continue` idempotently appends the pending Insight and clears the stale
  journal before starting the next round;
- an infrastructure-incomplete round retains the journal and writes neither history nor Insight;
- static mode stores `"insight": null` and never creates `insights.jsonl`.

- [ ] **Step 6: Move journal clearing to the business-commit boundary**

Remove this code from `HEPJobBackend._collect()`:

```python
if self._journal is not None:
    self._journal.clear()
```

In the outer loop, carry `round_insight` from either the fresh `ProposerResult` or inflight metadata. After candidate finalization and selection:

```python
ctx.store.append_generation(
    round_id,
    parent_sha=parent_sha,
    selected_candidate=selected_candidate,
    selected_sha=selected_sha,
    candidates=candidates,
    telemetry=ctx.telemetry.snapshot(persist=True),
)
if round_insight is not None:
    ctx.insight_store.append(round_id, round_insight)
journal.clear()
_refresh_progress_plot(ctx.store, ctx.telemetry.plot_context())
```

If all candidate infrastructure jobs fail, return before these lines, preserving the inflight file. Do not add cross-file transactions or rollback code in this MVP.

Because history and Insights are separate append-only files, add one narrow
recovery rule before the normal inflight round-id check: when the inflight
round is exactly the most recently recorded history round, append its Insight
idempotently and clear the journal. Any other round-id mismatch remains an
error. This is current crash recovery, not old-run compatibility.

- [ ] **Step 7: Verify integration and commit**

Run:

```bash
python -m pytest -q \
  tests/test_static_mode.py \
  tests/test_parallel_candidates.py \
  tests/test_hepjob_backend.py \
  tests/test_provenance_export_lock.py
```

Expected: PASS.

```bash
git add simpleloop/loop.py simpleloop/execution/hepjob.py tests/test_static_mode.py tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_provenance_export_lock.py
git commit -m "refactor: integrate proposer agent into minimal loop"
```

---

## Task 6: Publish the minimal user contract and run the full audit

**Necessity:** Users need exactly three new facts: how to configure the Researcher, where to place the key, and how factual history differs from Insight memory. Updating runnable examples prevents a normal example run from failing preflight. No provider matrix or speculative extension documentation is added.

**Files:**

- Modify: `README.md`
- Modify: `examples/README.md`
- Modify: `examples/tiny_algo_opt/README.md`
- Modify: `examples/omilrec-opt/README.md`
- Modify: `examples/omilrec-post-v107-opt/README.md`
- Modify: `examples/omilrec-v100-opt/README.md`
- Modify: `examples/task.yaml`
- Modify: `examples/tiny_algo_opt/task.yaml`
- Modify: `examples/tiny_algo_opt/task_hepjob.yaml`
- Modify: `examples/omilrec-opt/task.yaml`
- Modify: `examples/omilrec-post-v107-opt/task.yaml`
- Modify: `examples/omilrec-post-v107-opt/task_hints.yaml`
- Modify: `examples/omilrec-post-v107-opt/task_nohints.yaml`
- Modify: `examples/omilrec-v100-opt/task.yaml`
- Modify: `examples/omilrec-v100-opt/task_hints.yaml`
- Modify: `examples/omilrec-v100-opt/task_nohints.yaml`

- [ ] **Step 1: Add the exact Researcher block to agent-driven examples**

```yaml
researcher:
  api: hepai
  model: gpt-5.5
  base_url: https://aiapi.ihep.ac.cn/apiv2
  max_steps: 20
  command_timeout_seconds: 120
  command_output_cap_chars: 12000
```

Do not add the API key to YAML. Document:

```bash
export HEPAI_API_KEY='<your-key>'
simpleloop run --config examples/task.yaml
```

- [ ] **Step 2: Document behavior and authority in one compact section**

README content must state:

- the outer loop remains Proposer → Executor → Harness;
- the Proposer may freely research and propose broad rewrites but cannot edit candidates or decide facts/gates;
- the Executor implements proposals and returns committed candidates;
- Harness eval plus changed-path/physical gates determine eligibility and parent candidates;
- recent full facts plus all compact Insights are injected; older factual episodes are searched/inspected on demand;
- Insights are Proposer hypotheses/indexes, never authoritative facts;
- research Bash is read-only/offline/temporary;
- `--proposals` static mode bypasses HEPAI and Insights.

- [ ] **Step 3: Run targeted secret and schema checks**

Run:

```bash
rg -n "sk-[A-Za-z0-9]|HEPAI_API_KEY:" simpleloop examples README.md pyproject.toml
python -m pytest -q tests/test_config_execution.py tests/test_runtime.py tests/test_static_mode.py
```

Expected: no embedded key; tests PASS.

- [ ] **Step 4: Run the complete regression suite**

Run: `python -m pytest -q tests`

Expected: all tests PASS (baseline before implementation: 323 passed).

- [ ] **Step 5: Perform the MVP deletion audit**

Run:

```bash
rg -n "judger" simpleloop tests
rg -n "fallback|provider_registry|native_tool|legacy_insight|retry" \
  simpleloop/roles/model.py simpleloop/roles/proposer.py \
  simpleloop/roles/research_tools.py tests/test_model.py \
  tests/test_proposer_agent.py tests/test_research_tools.py
git diff --stat refactor/open-researcher-loop...HEAD
git diff --check
```

Manually verify:

- no Judger or direct Proposer → Worker path exists;
- no old one-shot `propose()` / `_proposer_schema()` path remains;
- no compatibility `if/else` for old Insights or old dynamic runs exists;
- no HEPAI key is forwarded to Apptainer or HEPJob;
- every new config field is consumed;
- tests correspond to an active contract and contain no obsolete one-shot fixtures;
- additions are limited to model boundary, agent loop, research tools/sandbox, Insight store, and required integration/docs.

- [ ] **Step 6: Commit documentation and examples**

```bash
git add README.md examples tests
git commit -m "docs: document scientific proposer runtime"
```

- [ ] **Step 7: Final verification record**

Run:

```bash
git status --short
git log --oneline --decorate -7
python -m pytest -q tests
```

Expected: clean worktree and full suite PASS. Record the exact pass count and diff stat in the handoff; do not claim completion from earlier test output.

---

## Requirement-to-Code Map

| Requirement | Minimal implementation |
|---|---|
| Scientific identity without fixed workflow | Editable `prompts/proposer.md` epistemic principles |
| Enforced permissions and terminal contract | Immutable action protocol in `roles/proposer.py` |
| Adaptive investigation | Bounded JSON-action loop |
| Read code/Git/run analysis | One sandboxed `run_research_command` action |
| Full factual history without full injection | `search_history` + `inspect_episode` |
| Compact semantic memory | One replaceable pending Insight, one append per completed round |
| Objective truth and admission | Existing Harness eval/gates/selection, unchanged |
| Resume without re-proposing | Proposal + pending Insight in existing inflight journal |
| HEPAI first, multiple APIs later | `ChatModel` protocol + one `HepAIChatModel`, no registry |
| Static controlled experiments | Existing static mode bypasses model and Insights |

## Explicit Non-Goals for This Branch

- Proposer calling Executor/Worker or Harness directly.
- Removing the outer loop.
- Harness evolution or self-editing gates.
- Multiple model providers, routing, fallback, or retries.
- Native function calling in addition to JSON actions.
- Web/literature tools or network access in research Bash.
- Persistent shell sessions or scratch state.
- Insight scoring, merging, summarizing agents, or legacy migration.
- A new Judger, Reviewer, or role hierarchy.
