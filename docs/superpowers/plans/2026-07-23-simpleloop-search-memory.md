# SimpleLoop Search Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-run append-only Insights and exact historical candidate lookup so the existing Proposer can learn during reflection while keeping its structured output and prompt small.

**Architecture:** Keep `history.jsonl` as complete episodic evidence and add `insights.jsonl` as a three-field append-only semantic index. A focused `simpleloop.memory` module normalizes `r<round>c<candidate>` references, resolves compact episodes, validates and stores Insights, and renders them for the Proposer; the loop passes all Insights plus only the most recent six detailed rounds, then persists at most one validated Insight after recording the generation.

**Tech Stack:** Python 3.9+, dataclasses, JSONL, argparse, pytest, existing SimpleLoop Agent structured-output adapter.

## Global Constraints

- Work directly on branch `v0.0.4`; do not create a worktree or switch branches.
- Preserve `history.jsonl` as the full append-only experimental source of truth.
- Store Insights in `runs/<run>/insights.jsonl` with exactly `id`, `text`, and `refs`.
- Use candidate references `r<round>c<candidate>`; normalize legacy serial rounds as candidate zero.
- Create at most one Insight per Proposer call; an empty Insight writes nothing.
- Do not implement actions, revision, retirement, confidence, hypothesis states, embeddings, semantic search, extra Agents, or extra model calls.
- Show all compact Insights and only the most recent six detailed round records to the Proposer.
- Keep `reflection`, `insight`, `insight_refs`, and `decision` as parallel prompt-contract entries; show `["r0c0", "r1c1"]` as the reference example.
- Express `insight` as the smallest reusable conclusion in one or two concise, generalizing sentences.
- Invalid Insight references warn and skip only the Insight; they must not discard candidate generation.
- Preserve existing Executor, Judger, gates, candidate selection, accepted lineage, static-proposal mode, and existing-run compatibility.

---

### Task 1: Episodic Reference Lookup and Append-Only Insight Store

**Files:**
- Create: `simpleloop/memory.py`
- Create: `simpleloop/tests/test_memory.py`
- Modify: `simpleloop/cli.py`

**Interfaces:**
- Produces: `read_history(path: Path) -> list[dict]`
- Produces: `resolve_episode(history: list[dict], ref: str) -> dict`
- Produces: `load_insights(path: Path) -> list[dict]`
- Produces: `render_insights(insights: list[dict]) -> str`
- Produces: `validate_insight(text: str, refs: list[str], history: list[dict]) -> tuple[str, list[str]] | None`
- Produces: `append_insight(path: Path, round_id: int, text: str, refs: list[str]) -> bool`
- Produces CLI: `simpleloop memory show <ref> [--run-dir <path>]`
- Consumes: existing serial and parallel `history.jsonl` record shapes.

- [ ] **Step 1: Write failing tests for candidate-reference normalization**

Create `simpleloop/tests/test_memory.py` with tests covering parallel and
legacy serial records:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop import memory


def _parallel_history() -> list[dict]:
    return [{
        "round": 2,
        "parent_sha": "parent",
        "selected_candidate": 1,
        "selected_sha": "sha-1",
        "candidates": [
            {
                "candidate": 0,
                "family": "hoist",
                "proposal": "hoist lookup",
                "sha": "sha-0",
                "selected": False,
                "accepted": True,
                "metrics": {"SPEED_MS": 120.0},
                "risk": "low",
                "feedback": "slower",
                "changed_paths": ["src/a.cc"],
                "eval_block": "must not be returned",
            },
            {
                "candidate": 1,
                "family": "layout",
                "proposal": "pack values",
                "sha": "sha-1",
                "selected": True,
                "accepted": True,
                "metrics": {"SPEED_MS": 90.0},
                "risk": "low",
                "feedback": "faster",
                "changed_paths": ["src/b.cc"],
                "eval_block": "must not be returned",
            },
        ],
    }]


def test_resolve_parallel_episode_returns_only_compact_fields():
    episode = memory.resolve_episode(_parallel_history(), "r2c1")

    assert episode == {
        "ref": "r2c1",
        "family": "layout",
        "proposal": "pack values",
        "parent_sha": "parent",
        "candidate_sha": "sha-1",
        "selected": True,
        "accepted": True,
        "metrics": {"SPEED_MS": 90.0},
        "risk": "low",
        "feedback": "faster",
        "changed_paths": ["src/b.cc"],
    }
    assert "eval_block" not in episode


def test_resolve_serial_episode_normalizes_candidate_zero():
    history = [{
        "round": 7,
        "proposal": "legacy proposal",
        "sha": "legacy-sha",
        "accepted": True,
        "base_sha": "legacy-sha",
        "score": 0.7,
        "risk": "low",
        "metrics": {"SPEED_MS": 100.0},
        "feedback": "legacy feedback",
        "changed_paths": ["src/legacy.cc"],
    }]

    episode = memory.resolve_episode(history, "r7c0")

    assert episode["ref"] == "r7c0"
    assert episode["proposal"] == "legacy proposal"
    assert episode["candidate_sha"] == "legacy-sha"
    assert episode["selected"] is True


@pytest.mark.parametrize("ref", ["r2", "2c1", "r-1c0", "r2c-1", "r2c9"])
def test_resolve_episode_rejects_invalid_or_missing_refs(ref: str):
    with pytest.raises(ValueError):
        memory.resolve_episode(_parallel_history(), ref)
```

- [ ] **Step 2: Run reference tests and verify they fail**

Run:

```bash
python -m pytest simpleloop/tests/test_memory.py -k resolve -v
```

Expected: collection fails because `simpleloop.memory` does not exist.

- [ ] **Step 3: Implement reference parsing, history loading, and compact resolution**

Create `simpleloop/memory.py` with:

```python
from __future__ import annotations

import json
import re
from pathlib import Path


_EPISODE_REF_RE = re.compile(r"^r(0|[1-9]\d*)c(0|[1-9]\d*)$")


def read_history(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read history memory {path}: {exc}") from exc


def _parse_episode_ref(ref: str) -> tuple[int, int]:
    match = _EPISODE_REF_RE.fullmatch(str(ref).strip())
    if match is None:
        raise ValueError(
            f"invalid memory reference {ref!r}; expected r<round>c<candidate>"
        )
    return int(match.group(1)), int(match.group(2))


def resolve_episode(history: list[dict], ref: str) -> dict:
    round_id, candidate_id = _parse_episode_ref(ref)
    record = next(
        (item for item in history if item.get("round") == round_id),
        None,
    )
    if record is None:
        raise ValueError(f"memory reference not found: {ref}")

    if "candidates" in record:
        candidate = next(
            (
                item for item in (record.get("candidates") or [])
                if item.get("candidate") == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise ValueError(f"memory reference not found: {ref}")
        parent_sha = record.get("parent_sha")
    else:
        if candidate_id != 0:
            raise ValueError(f"memory reference not found: {ref}")
        candidate = record
        parent_sha = record.get("parent_sha")

    return {
        "ref": f"r{round_id}c{candidate_id}",
        "family": candidate.get("family") or "single",
        "proposal": candidate.get("proposal") or "",
        "parent_sha": parent_sha,
        "candidate_sha": candidate.get("sha"),
        "selected": (
            bool(candidate.get("selected"))
            if "candidates" in record else bool(candidate.get("accepted"))
        ),
        "accepted": bool(candidate.get("accepted")),
        "metrics": candidate.get("metrics") or {},
        "risk": candidate.get("risk"),
        "feedback": candidate.get("feedback") or "",
        "changed_paths": candidate.get("changed_paths") or [],
    }
```

- [ ] **Step 4: Run reference tests and verify they pass**

Run:

```bash
python -m pytest simpleloop/tests/test_memory.py -k resolve -v
```

Expected: all reference tests pass.

- [ ] **Step 5: Write failing tests for Insight validation, persistence, rendering, and corruption**

Append to `simpleloop/tests/test_memory.py`:

```python
def test_validate_insight_accepts_grounded_text_and_normalizes_refs():
    validated = memory.validate_insight(
        "  Sparse gathers benefit from packing.  ",
        [" r2c0 ", "r2c1"],
        _parallel_history(),
    )
    assert validated == (
        "Sparse gathers benefit from packing.",
        ["r2c0", "r2c1"],
    )


def test_validate_insight_treats_empty_text_as_no_write():
    assert memory.validate_insight("", [], _parallel_history()) is None


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("lesson", []),
        ("", ["r2c0"]),
        ("lesson", ["r99c0"]),
        ("lesson", ["bad-ref"]),
    ],
)
def test_validate_insight_rejects_inconsistent_or_unknown_refs(text, refs):
    with pytest.raises(ValueError):
        memory.validate_insight(text, refs, _parallel_history())


def test_append_and_load_insight_are_append_only_and_idempotent(tmp_path: Path):
    path = tmp_path / "insights.jsonl"
    assert memory.append_insight(
        path, 31, "A compact lesson.", ["r2c0", "r2c1"]
    ) is True
    assert memory.append_insight(
        path, 31, "A compact lesson.", ["r2c0", "r2c1"]
    ) is False
    assert memory.load_insights(path) == [{
        "id": "I31",
        "text": "A compact lesson.",
        "refs": ["r2c0", "r2c1"],
    }]


def test_append_insight_rejects_conflicting_same_round(tmp_path: Path):
    path = tmp_path / "insights.jsonl"
    memory.append_insight(path, 4, "first", ["r2c0"])
    with pytest.raises(ValueError, match="conflicting insight I4"):
        memory.append_insight(path, 4, "different", ["r2c1"])


def test_load_insights_fails_clearly_on_corrupt_json(tmp_path: Path):
    path = tmp_path / "insights.jsonl"
    path.write_text("{bad json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not read insight memory"):
        memory.load_insights(path)


def test_render_insights_is_compact_and_includes_evidence():
    rendered = memory.render_insights([{
        "id": "I31",
        "text": "Dense copying only helped the sparse gather.",
        "refs": ["r29c0", "r30c0"],
    }])
    assert rendered == (
        "[I31] Dense copying only helped the sparse gather.\n"
        "Evidence: r29c0, r30c0"
    )


def test_render_empty_insights_has_explicit_first_run_message():
    assert memory.render_insights([]) == "  (none yet)"
```

- [ ] **Step 6: Run Insight tests and verify they fail**

Run:

```bash
python -m pytest simpleloop/tests/test_memory.py -k 'insight or render' -v
```

Expected: failures because the Insight functions do not exist.

- [ ] **Step 7: Implement the minimal append-only Insight functions**

Add to `simpleloop/memory.py`:

```python
def load_insights(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read insight memory {path}: {exc}") from exc
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"id", "text", "refs"}
            or not isinstance(record["id"], str)
            or not isinstance(record["text"], str)
            or not isinstance(record["refs"], list)
            or not all(isinstance(ref, str) for ref in record["refs"])
        ):
            raise ValueError(f"invalid insight memory record in {path}: {record!r}")
    return records


def render_insights(insights: list[dict]) -> str:
    if not insights:
        return "  (none yet)"
    return "\n\n".join(
        f"[{item['id']}] {item['text']}\n"
        f"Evidence: {', '.join(item['refs'])}"
        for item in insights
    )


def validate_insight(
    text: str,
    refs: list[str],
    history: list[dict],
) -> tuple[str, list[str]] | None:
    normalized_text = str(text).strip()
    normalized_refs = [str(ref).strip() for ref in refs]
    if not normalized_text:
        if normalized_refs:
            raise ValueError("empty insight must have empty insight_refs")
        return None
    if not normalized_refs:
        raise ValueError("non-empty insight requires at least one insight_ref")
    for ref in normalized_refs:
        resolve_episode(history, ref)
    return normalized_text, normalized_refs


def append_insight(
    path: Path,
    round_id: int,
    text: str,
    refs: list[str],
) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": f"I{round_id}", "text": text, "refs": refs}
    existing = load_insights(path)
    same_id = next(
        (item for item in existing if item["id"] == record["id"]),
        None,
    )
    if same_id == record:
        return False
    if same_id is not None:
        raise ValueError(f"conflicting insight {record['id']}")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True
```

- [ ] **Step 8: Run all memory-unit tests**

Run:

```bash
python -m pytest simpleloop/tests/test_memory.py -v
```

Expected: all tests pass.

- [ ] **Step 9: Write failing CLI tests**

Append to `simpleloop/tests/test_memory.py`:

```python
from simpleloop import cli


def test_memory_show_cli_resolves_from_explicit_run_dir(
    tmp_path: Path, capsys
):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(_parallel_history()[0]) + "\n",
        encoding="utf-8",
    )

    cli.main([
        "memory", "show", "r2c1",
        "--run-dir", str(tmp_path),
    ])

    output = json.loads(capsys.readouterr().out)
    assert output["ref"] == "r2c1"
    assert output["candidate_sha"] == "sha-1"
    assert "eval_block" not in output


def test_memory_show_cli_discovers_run_from_repo_cwd(
    tmp_path: Path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "history.jsonl").write_text(
        json.dumps(_parallel_history()[0]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    cli.main(["memory", "show", "r2c0"])

    assert json.loads(capsys.readouterr().out)["ref"] == "r2c0"
```

- [ ] **Step 10: Run CLI tests and verify they fail**

Run:

```bash
python -m pytest simpleloop/tests/test_memory.py -k cli -v
```

Expected: argparse rejects the unknown `memory` command.

- [ ] **Step 11: Add the nested `memory show` CLI**

Modify `simpleloop/cli.py`:

```python
import json

from . import memory
```

Add parsers before `args = parser.parse_args(argv)`:

```python
    memory_parser = sub.add_parser(
        "memory", help="Inspect current-run Search Memory."
    )
    memory_sub = memory_parser.add_subparsers(
        dest="memory_command", required=True
    )
    memory_show = memory_sub.add_parser(
        "show", help="Show one historical candidate by r<round>c<candidate> ref."
    )
    memory_show.add_argument("ref")
    memory_show.add_argument("--run-dir")
```

Handle the command before `validate`:

```python
    if args.command == "memory":
        if args.run_dir:
            run_dir = Path(args.run_dir).expanduser().resolve()
        else:
            cwd = Path.cwd()
            run_dir = cwd if (cwd / "history.jsonl").exists() else cwd.parent
        history_path = run_dir / "history.jsonl"
        if not history_path.exists():
            print(
                f"Error: no history.jsonl for current run at {run_dir}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        try:
            episode = memory.resolve_episode(
                memory.read_history(history_path),
                args.ref,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(episode, ensure_ascii=False, indent=2))
        return
```

- [ ] **Step 12: Run memory and CLI tests**

Run:

```bash
python -m pytest simpleloop/tests/test_memory.py -v
```

Expected: all tests pass.

- [ ] **Step 13: Commit Task 1**

```bash
git add simpleloop/memory.py simpleloop/cli.py simpleloop/tests/test_memory.py
git commit -m "feat: add append-only search memory store"
```

---

### Task 2: Proposer Insight Contract and Bounded Context

**Files:**
- Modify: `simpleloop/proposer.py`
- Modify: `simpleloop/views.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`
- Modify: `simpleloop/tests/test_views_and_parse.py`

**Interfaces:**
- Consumes: `memory.render_insights(insights: list[dict]) -> str`
- Changes: `ProposalBatch(reflection, insight, insight_refs, proposals)`
- Changes: `propose(..., history: list[dict], insights: list[dict], ...)`
- Produces: schema fields `reflection`, `insight`, `insight_refs`, `proposals`
- Produces: `views.for_proposer(history)` containing only the final six records,
  with complete proposal text.

- [ ] **Step 1: Update schema and parser tests first**

Modify proposer test inputs in
`simpleloop/tests/test_parallel_candidates.py` so every valid response includes:

```python
{
    "reflection": "r",
    "insight": "",
    "insight_refs": [],
    "proposals": [
        {"family": "layout", "decision": "switch", "proposal": "p0"},
    ],
}
```

Add focused tests:

```python
def test_proposer_schema_requires_minimal_insight_fields():
    schema = _proposer_schema(1)
    assert schema["required"] == [
        "reflection", "insight", "insight_refs", "proposals"
    ]
    assert schema["properties"]["insight"]["type"] == "string"
    assert schema["properties"]["insight_refs"]["type"] == "array"


def test_parse_batch_keeps_insight_and_refs():
    batch = _parse_batch(
        {
            "reflection": "recent evidence changed the search",
            "insight": "Sparse gathers benefit from packing.",
            "insight_refs": ["r0c0", "r1c1"],
            "proposals": [{
                "family": "layout",
                "decision": "switch",
                "proposal": "inspect another sparse gather",
            }],
        },
        candidates_per_round=1,
    )
    assert batch.insight == "Sparse gathers benefit from packing."
    assert batch.insight_refs == ["r0c0", "r1c1"]


def test_parse_batch_rejects_missing_insight_fields():
    with pytest.raises(ValueError, match="reflection, insight, insight_refs"):
        _parse_batch(
            {"reflection": "r", "proposals": []},
            candidates_per_round=1,
        )
```

- [ ] **Step 2: Run proposer schema/parser tests and verify they fail**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  -k 'proposer_schema or parse_batch' -v
```

Expected: failures because the current contract has only `reflection` and
`proposals`.

- [ ] **Step 3: Implement the minimal structured-output additions**

In `simpleloop/proposer.py`:

```python
_INSIGHT_GENERATION_LIMIT = 500
_INSIGHT_REF_GENERATION_LIMIT = 32
```

Change `ProposalBatch`:

```python
@dataclass
class ProposalBatch:
    reflection: str
    insight: str
    insight_refs: list[str]
    proposals: list[Proposal]
```

Extend `_proposer_schema`:

```python
"required": ["reflection", "insight", "insight_refs", "proposals"],
"properties": {
    "reflection": {
        "type": "string",
        "maxLength": _REFLECTION_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
    },
    "insight": {
        "type": "string",
        "maxLength": _INSIGHT_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
    },
    "insight_refs": {
        "type": "array",
        "items": {
            "type": "string",
            "maxLength": (
                _INSIGHT_REF_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN
            ),
        },
    },
    "proposals": {
        "type": "array",
        "minItems": candidates_per_round,
        "maxItems": candidates_per_round,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["family", "decision", "proposal"],
            "properties": {
                "family": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _FAMILY_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
                    "pattern": r"\S",
                },
                "decision": {
                    "type": "string",
                    "enum": ["continue", "switch"],
                },
                "proposal": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _PROPOSAL_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
                    "pattern": r"\S",
                },
            },
        },
    },
}
```

Change `_parse_batch` to require exactly the four top-level keys, validate
`insight` as a string and `insight_refs` as a list of strings, trim them, and
return them on `ProposalBatch`. Do not validate reference existence here;
invalid Insight memory must not abort candidate generation.

- [ ] **Step 4: Run proposer schema/parser tests**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  -k 'proposer_schema or parse_batch' -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Replace old-history compaction tests with recent-six tests**

In `simpleloop/tests/test_views_and_parse.py`, replace tests that expect
`proposal_head` with:

```python
def test_for_proposer_returns_only_last_six_records_with_full_proposals():
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in [3, 5, 12, 20, 21, 40, 99]
    ]

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == [5, 12, 20, 21, 40, 99]
    assert [row["proposal"] for row in projected] == [
        "proposal-5", "proposal-12", "proposal-20",
        "proposal-21", "proposal-40", "proposal-99",
    ]
    assert all("proposal_head" not in row for row in projected)


def test_for_proposer_recent_window_uses_record_position_and_does_not_mutate():
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in [3, 5, 12, 20, 21, 40, 99]
    ]
    original = deepcopy(history)

    projected = views.for_proposer(history)

    assert projected[0]["round"] == 5
    assert history == original


def test_for_proposer_keeps_all_candidates_in_recent_generation():
    history = [
        _serial_history_record(i, f"old-{i}") for i in range(2)
    ]
    history.extend(
        _parallel_history_record(i, [f"p{i}-0", f"p{i}-1"])
        for i in range(2, 8)
    )

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == [2, 3, 4, 5, 6, 7]
    assert projected[-1]["candidates"][1]["proposal"] == "p7-1"
```

- [ ] **Step 6: Run view tests and verify old behavior fails**

Run:

```bash
python -m pytest simpleloop/tests/test_views_and_parse.py \
  -k for_proposer -v
```

Expected: recent-window tests fail because all records are still projected.

- [ ] **Step 7: Make `views.for_proposer` a recent-six full projection**

In `simpleloop/views.py`:

- remove `_PROPOSER_OLD_PROPOSAL_CHARS`;
- remove `_proposal_projection`;
- keep `_PROPOSER_FULL_PROPOSAL_ROUNDS = 6`, renaming it to
  `_PROPOSER_RECENT_ROUNDS = 6`;
- iterate over `history[-_PROPOSER_RECENT_ROUNDS:]`;
- always emit `"proposal": ...` for serial and candidate records;
- keep all other role-visible fields and continue excluding `eval_block`.

- [ ] **Step 8: Run all view tests**

Run:

```bash
python -m pytest simpleloop/tests/test_views_and_parse.py \
  -k for_proposer -v
```

Expected: all selected tests pass.

- [ ] **Step 9: Write a failing prompt test with Insights and exact wording**

Update `test_proposer_passes_hard_schema_and_keeps_prompt_semantic` so the fake
response includes `insight` and `insight_refs`, call `propose` with:

```python
insights=[{
    "id": "I4",
    "text": "Hoisting behind cold gates measured as noise.",
    "refs": ["r0c0", "r3c0"],
}]
```

Add assertions:

```python
assert "Accumulated search insights:" in prompt
assert "[I4] Hoisting behind cold gates measured as noise." in prompt
assert "Evidence: r0c0, r3c0" in prompt
assert 'simpleloop memory show <ref>' in prompt
assert '`reflection`:' in agent.prompt
assert '`insight`:' in agent.prompt
assert '`insight_refs`:' in agent.prompt
assert '`decision`:' in agent.prompt
assert '["r0c0", "r1c1"]' in agent.prompt
assert "one or two concise, generalizing sentences" in prompt
assert (
    "previous evidence -> reflection -> optional insight "
    "-> decision -> proposal"
) in prompt
```

- [ ] **Step 10: Run the prompt test and verify it fails**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  -k proposer_passes_hard_schema_and_keeps_prompt_semantic -v
```

Expected: failure because `propose` does not accept or render Insights.

- [ ] **Step 11: Integrate compact Insights and approved soft reflection wording**

Modify `simpleloop/proposer.py`:

- import `.memory`;
- add `insights: list[dict]` to `propose`;
- render them with `memory.render_insights(insights)`;
- insert an `Accumulated search insights:` block before recent outcomes;
- describe `simpleloop memory show <ref>` as optional retrieval when an
  important Insight is too compact, conflicts with recent evidence, may no
  longer match current source, or supports revisiting an old direction;
- replace the current output-chain block with the approved wording from
  `docs/superpowers/specs/2026-07-23-simpleloop-search-memory-design.md`,
  keeping `reflection`, `insight`, `insight_refs`, and `decision` as peer
  entries and retaining the existing `proposal` entry;
- include the exact `insight_refs` example `["r0c0", "r1c1"]`;
- describe `insight` as its smallest reusable form in one or two concise,
  generalizing sentences;
- keep the language advisory rather than adding semantic retries or mandatory
  Insight creation.

- [ ] **Step 12: Run proposer and view tests**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_views_and_parse.py -v
```

Expected: all tests in both files pass.

- [ ] **Step 13: Commit Task 2**

```bash
git add simpleloop/proposer.py simpleloop/views.py \
  simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_views_and_parse.py
git commit -m "feat: teach proposer from compact search insights"
```

---

### Task 3: Loop Validation, Persistence, Resume, and Failure Isolation

**Files:**
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`
- Modify: `simpleloop/tests/test_memory.py`

**Interfaces:**
- Consumes: `memory.load_insights`, `memory.validate_insight`,
  `memory.append_insight`
- Consumes: `ProposalBatch.insight` and `ProposalBatch.insight_refs`
- Produces: at most one `I<round>` record after the corresponding generation
  is stored.

- [ ] **Step 1: Write failing integration tests for valid and invalid Insights**

Add loop-focused tests to `simpleloop/tests/test_parallel_candidates.py` using
the existing fake `Agent`, `Workspace`, `Store`, `_run_candidates`, and
`_select_winner` patterns.

The valid case must assert:

```python
def fake_propose(*_args, **kwargs):
    assert kwargs["insights"] == []
    assert [row["round"] for row in kwargs["history"]] == [0]
    return ProposalBatch(
        reflection="historical result narrows the useful mechanism",
        insight="Sparse gathers benefit from packing.",
        insight_refs=["r0c0"],
        proposals=[Proposal(
            family="layout",
            decision="continue",
            proposal="test another sparse gather",
        )],
    )
```

After the generation, assert `insights.jsonl` contains:

```python
{
    "id": "I1",
    "text": "Sparse gathers benefit from packing.",
    "refs": ["r0c0"],
}
```

The invalid-reference case returns `insight_refs=["r99c0"]` and asserts:

- `_run_candidates` still runs;
- the generation is recorded;
- `insights.jsonl` is absent or unchanged;
- stdout contains a warning describing the invalid Insight ref.

- [ ] **Step 2: Run the new loop integration tests and verify they fail**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  -k 'persists_valid_insight or skips_invalid_insight' -v
```

Expected: failures because the loop neither loads nor writes Insights.

- [ ] **Step 3: Add minimal Insight lifecycle to the dynamic proposer path**

Modify `simpleloop/loop.py`:

```python
from . import memory as memory_mod
```

After constructing `Store`:

```python
    insights_path = run_dir_path / "insights.jsonl"
```

At the start of each dynamic round:

```python
            history = store.history()
            insights = memory_mod.load_insights(insights_path)
```

Pass both to the Proposer:

```python
                proposal_obj = proposer_mod.propose(
                    ...,
                    history=history,
                    insights=insights,
                    ...
                )
```

Validate without aborting candidate generation:

```python
            pending_insight = None
            try:
                pending_insight = memory_mod.validate_insight(
                    proposal_obj.insight,
                    proposal_obj.insight_refs,
                    history,
                )
            except ValueError as exc:
                print(
                    f"[{stamp()}] insight skipped: {exc}",
                    flush=True,
                )
```

After `store.append_generation(...)`:

```python
            if pending_insight is not None:
                insight_text, insight_refs = pending_insight
                memory_mod.append_insight(
                    insights_path,
                    round_id,
                    insight_text,
                    insight_refs,
                )
```

Keep static-proposal mode unchanged and do not load, validate, or write an
Insight there.

- [ ] **Step 4: Run loop Insight integration tests**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  -k 'persists_valid_insight or skips_invalid_insight' -v
```

Expected: both tests pass.

- [ ] **Step 5: Add resume and corrupted-memory tests**

Add tests that verify:

1. `--continue` loads existing `insights.jsonl` and passes its records to the
   resumed Proposer.
2. An existing `I<round>` with identical content remains idempotent.
3. Corrupted `insights.jsonl` raises a clear `ValueError` before the Proposer
   call.
4. A run with no `insights.jsonl` passes an empty Insight list.

Use a fake Proposer that captures its `insights` keyword and a fake candidate
path that records whether execution began.

- [ ] **Step 6: Run resume/corruption tests and verify failures where expected**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py \
  -k 'continue_loads_insights or corrupt_insights or no_insights_file' -v
```

Expected before any required correction: failures identify missing resume or
corruption behavior; after the Task 3 implementation, all selected tests pass.

- [ ] **Step 7: Update affected fake Proposers and direct calls**

Search:

```bash
rg -n "propose\\(|ProposalBatch\\(" simpleloop simpleloop/tests
```

Update every direct `propose` call to pass `insights=[]` and every
`ProposalBatch` construction to include:

```python
insight=""
insight_refs=[]
```

Update fake structured Proposer responses to include:

```python
"insight": ""
"insight_refs": []
```

- [ ] **Step 8: Run the complete SimpleLoop test suite**

Run:

```bash
python -m pytest simpleloop/tests -v
```

Expected: all tests pass with zero failures.

- [ ] **Step 9: Exercise the real CLI help and exact-ref lookup**

Run:

```bash
python -m simpleloop.cli memory show --help
```

Expected: help describes `<ref>` and optional `--run-dir`.

Create no permanent fixture files; use an existing run read-only:

```bash
python -m simpleloop.cli memory show r0c0 \
  --run-dir runs/tiny-algo-parallel-002
```

Expected: one JSON candidate episode, no raw `eval_block`, and no unrelated
rounds.

- [ ] **Step 10: Verify branch, diff, and formatting**

Run:

```bash
git branch --show-current
git diff --check
git status --short
```

Expected:

- branch is `v0.0.4`;
- `git diff --check` exits zero;
- only intended implementation/test/plan files are modified.

- [ ] **Step 11: Commit Task 3**

```bash
git add simpleloop/loop.py simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_memory.py
git commit -m "feat: persist proposer insights across loop rounds"
```

---

### Task 4: Final Verification and Documentation Consistency

**Files:**
- Modify only if verification exposes a mismatch:
  `docs/superpowers/specs/2026-07-23-simpleloop-search-memory-design.md`
- Modify only if CLI discoverability is missing: `README.md`

**Interfaces:**
- Consumes all Task 1-3 behavior.
- Produces a verified `v0.0.4` branch with implementation and tests aligned to
  the approved spec.

- [ ] **Step 1: Run the full unit suite fresh**

Run:

```bash
python -m pytest simpleloop/tests -v
```

Expected: zero failures.

- [ ] **Step 2: Verify every approved prompt-contract detail**

Run:

```bash
rg -n \
  'reflection|insight_refs|r0c0|r1c1|one or two concise, generalizing sentences|simpleloop memory show' \
  simpleloop/proposer.py
```

Expected: the current prompt contains all four peer reasoning fields, the
reference examples, the concise Insight form, and the optional retrieval
command.

- [ ] **Step 3: Verify MVP exclusions did not enter the implementation**

Run:

```bash
rg -n \
  'learning.action|revise|retire|confidence|embedding|search_status|hypothesis_status' \
  simpleloop simpleloop/tests
```

Expected: no implementation of excluded Search Memory features.

- [ ] **Step 4: Verify commit scope and branch**

Run:

```bash
git branch --show-current
git status --short
git log -5 --oneline
```

Expected: branch is `v0.0.4`, implementation commits are present, and the
worktree is clean.

- [ ] **Step 5: Commit any verification-only documentation correction**

Only if Steps 1-4 required a documentation change:

```bash
git add README.md \
  docs/superpowers/specs/2026-07-23-simpleloop-search-memory-design.md
git commit -m "docs: align search memory usage"
```

Otherwise do not create an empty commit.
