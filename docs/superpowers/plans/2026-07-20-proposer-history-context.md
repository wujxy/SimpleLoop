# Proposer History Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 proposer prompt 中保留最近 6 个 round records 的完整 proposal，并将更早记录的 proposal 规范化后截取为最多 300 字符的 `proposal_head`。

**Architecture:** `history.jsonl` 继续作为完整、不可损失的事实源；上下文压缩只发生在 `views.for_proposer()` 的角色投影层。`proposer.propose()` 根据投影结果中的 `proposal` 或 `proposal_head` 渲染对应标签，串行记录和并行 generation records 使用同一套按记录位置计算的窗口。

**Tech Stack:** Python 3、pytest、SimpleLoop 现有 dict/JSONL history 模型。

## Global Constraints

- 最近窗口固定为 6 个 top-level round records，不增加 YAML 配置。
- 旧 proposal 正文固定为规范化后的前 300 个字符；仅在超长时追加单字符 `…`。
- recency 按 `history` 列表位置判断，不根据 `record["round"]` 数值计算。
- 一个并行 generation 是一个 round record；该 record 内所有 candidates 使用相同的新旧判定。
- 保留 SHA、accepted/selected/base state、metrics、score、risk、changed paths、feedback 和 `feedback_for_report`。
- 不修改输入 history，不修改持久化 schema，不压缩 `history.jsonl` 或 final report。
- `eval_block` 继续不进入 proposer 上下文。

---

## File Structure

- Modify: `simpleloop/views.py`
  - 定义窗口常量和 proposal 投影 helper。
  - 按 top-level record 位置对串行与并行 proposal 做完整/摘要投影。
- Modify: `simpleloop/proposer.py`
  - 将 `proposal`/`proposal_head` 投影渲染成显式、匹配的 prompt 标签。
- Modify: `simpleloop/tests/test_views_and_parse.py`
  - 覆盖窗口边界、非连续 round number、空白规范化、截断、并行 candidates、不可变性和 prompt 标签。
- Reference: `docs/superpowers/specs/2026-07-20-proposer-history-context-design.md`
  - 已确认的行为规范。

### Task 1: Add The Proposal Window To The Proposer View

**Files:**
- Modify: `simpleloop/views.py:24-94`
- Test: `simpleloop/tests/test_views_and_parse.py:26-80`

**Interfaces:**
- Consumes: `history: list[dict]`，其中每一项是 top-level round record。
- Produces: `for_proposer(history: list[dict]) -> list[dict]`；recent records 含 `proposal`，older records 含 `proposal_head`。
- Produces: `_proposal_projection(proposal: str, keep_full: bool) -> dict[str, str]`。

- [ ] **Step 1: Add serial-window tests that initially fail**

在 `simpleloop/tests/test_views_and_parse.py` 的 proposer view tests 中加入：

```python
from copy import deepcopy


def _serial_history_record(round_id: int, proposal: str) -> dict:
    return {
        "round": round_id,
        "proposal": proposal,
        "sha": f"sha-{round_id}",
        "accepted": True,
        "base_sha": f"sha-{round_id}",
        "score": 0.5,
        "risk": "low",
        "metrics": {"SPEED_MS": 500.0 + round_id},
        "changed_paths": ["src/a.cc"],
        "feedback": f"feedback-{round_id}",
        "feedback_for_report": f"diagnostic-{round_id}",
        "eval_block": "raw output",
    }


def test_for_proposer_keeps_last_six_records_full_by_position():
    round_ids = [3, 5, 12, 20, 21, 40, 99]
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in round_ids
    ]

    projected = views.for_proposer(history)

    assert projected[0]["round"] == 3
    assert projected[0]["proposal_head"] == "proposal-3"
    assert "proposal" not in projected[0]
    assert [row["round"] for row in projected[1:]] == round_ids[1:]
    assert [row["proposal"] for row in projected[1:]] == [
        f"proposal-{round_id}" for round_id in round_ids[1:]
    ]
    assert all("proposal_head" not in row for row in projected[1:])


def test_for_proposer_compacts_old_proposal_without_mutating_history():
    long_proposal = "  Compact\n\tthe   live list  " + ("x" * 320)
    history = [_serial_history_record(0, long_proposal)]
    history.extend(
        _serial_history_record(round_id, f"recent-{round_id}")
        for round_id in range(1, 7)
    )
    original = deepcopy(history)
    normalized = " ".join(long_proposal.split())

    projected = views.for_proposer(history)

    assert projected[0]["proposal_head"] == normalized[:300] + "…"
    assert len(projected[0]["proposal_head"]) == 301
    assert projected[0]["sha"] == "sha-0"
    assert projected[0]["metrics"] == {"SPEED_MS": 500.0}
    assert projected[0]["score"] == 0.5
    assert projected[0]["risk"] == "low"
    assert projected[0]["changed_paths"] == ["src/a.cc"]
    assert projected[0]["feedback"] == "feedback-0"
    assert projected[0]["feedback_for_report"] == "diagnostic-0"
    assert "eval_block" not in projected[0]
    assert history == original
```

- [ ] **Step 2: Run the serial-window tests and verify failure**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py::test_for_proposer_keeps_last_six_records_full_by_position \
  simpleloop/tests/test_views_and_parse.py::test_for_proposer_compacts_old_proposal_without_mutating_history \
  -v
```

Expected: FAIL because every projected serial record still contains `proposal`
and no record contains `proposal_head`.

- [ ] **Step 3: Add parallel-generation boundary tests that initially fail**

继续在同一测试文件中加入：

```python
def _parallel_history_record(round_id: int, proposals: list[str]) -> dict:
    return {
        "round": round_id,
        "parent_sha": f"parent-{round_id}",
        "selected_candidate": 1,
        "selected_sha": f"candidate-{round_id}-1",
        "base_sha": f"candidate-{round_id}-1",
        "reflection": "reflection",
        "candidates": [
            {
                "candidate": candidate_id,
                "family": f"family-{candidate_id}",
                "proposal": proposal,
                "sha": f"candidate-{round_id}-{candidate_id}",
                "selected": candidate_id == 1,
                "accepted": True,
                "score": 0.4 + candidate_id / 10,
                "risk": "low",
                "metrics": {"SPEED_MS": 600.0 - candidate_id},
                "changed_paths": [f"src/c{candidate_id}.cc"],
                "feedback": f"feedback-{candidate_id}",
                "feedback_for_report": f"diagnostic-{candidate_id}",
                "eval_block": "raw output",
            }
            for candidate_id, proposal in enumerate(proposals)
        ],
    }


def test_for_proposer_applies_one_window_state_to_all_generation_candidates():
    old_generation = _parallel_history_record(
        10,
        ["  old\n candidate zero  ", "old candidate one"],
    )
    recent_generation = _parallel_history_record(
        100,
        ["recent candidate zero", "recent candidate one"],
    )
    history = [old_generation]
    history.extend(
        _serial_history_record(round_id, f"recent-{round_id}")
        for round_id in [20, 30, 40, 50, 60]
    )
    history.append(recent_generation)

    projected = views.for_proposer(history)

    old_candidates = projected[0]["candidates"]
    assert [c["proposal_head"] for c in old_candidates] == [
        "old candidate zero",
        "old candidate one",
    ]
    assert all("proposal" not in c for c in old_candidates)
    assert old_candidates[1]["sha"] == "candidate-10-1"
    assert old_candidates[1]["selected"] is True
    assert old_candidates[1]["feedback_for_report"] == "diagnostic-1"
    assert all("eval_block" not in c for c in old_candidates)

    recent_candidates = projected[-1]["candidates"]
    assert [c["proposal"] for c in recent_candidates] == [
        "recent candidate zero",
        "recent candidate one",
    ]
    assert all("proposal_head" not in c for c in recent_candidates)
```

- [ ] **Step 4: Run the parallel-generation test and verify failure**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py::test_for_proposer_applies_one_window_state_to_all_generation_candidates \
  -v
```

Expected: FAIL with `KeyError: 'proposal_head'`.

- [ ] **Step 5: Implement the minimal view-layer projection**

在 `simpleloop/views.py` 中删除已经过时的 “sliding window not implemented”
模块说明，加入常量与 helper：

```python
_PROPOSER_FULL_PROPOSAL_ROUNDS = 6
_PROPOSER_OLD_PROPOSAL_CHARS = 300


def _proposal_projection(proposal: str, keep_full: bool) -> dict[str, str]:
    if keep_full:
        return {"proposal": proposal}
    normalized = " ".join(proposal.split())
    suffix = "…" if len(normalized) > _PROPOSER_OLD_PROPOSAL_CHARS else ""
    return {
        "proposal_head":
            normalized[:_PROPOSER_OLD_PROPOSAL_CHARS] + suffix,
    }
```

将 `for_proposer` 的循环改成按 top-level record 位置判断：

```python
    out = []
    full_proposal_start = max(
        0, len(history) - _PROPOSER_FULL_PROPOSAL_ROUNDS
    )
    for record_index, r in enumerate(history):
        keep_full_proposal = record_index >= full_proposal_start
```

并行 candidate dict 中用 unpack 替换固定的 `"proposal"` 字段：

```python
                        **_proposal_projection(
                            c.get("proposal") or "",
                            keep_full_proposal,
                        ),
```

串行 record dict 中同样使用：

```python
            **_proposal_projection(
                r.get("proposal") or "",
                keep_full_proposal,
            ),
```

其余投影字段保持原样。

- [ ] **Step 6: Run all proposer-view tests**

Run:

```bash
python -m pytest simpleloop/tests/test_views_and_parse.py -k for_proposer -v
```

Expected: all selected tests PASS, including existing empty-history,
order-preservation, and landing-state projection tests.

- [ ] **Step 7: Commit the view-layer behavior**

```bash
git add simpleloop/views.py simpleloop/tests/test_views_and_parse.py
git commit -m "feat: compact old proposer proposals"
```

### Task 2: Render Full And Compact Proposal Labels In The Prompt

**Files:**
- Modify: `simpleloop/proposer.py:66-118`
- Test: `simpleloop/tests/test_views_and_parse.py:377-416`

**Interfaces:**
- Consumes: a projected record or candidate containing exactly one of
  `proposal` and `proposal_head`.
- Produces: `_proposal_history_field(record: dict) -> tuple[str, str]` returning
  the label and text for prompt rendering.

- [ ] **Step 1: Add prompt-label coverage for serial and parallel old records**

在 `simpleloop/tests/test_views_and_parse.py` 中加入：

```python
def test_proposer_prompt_labels_compact_and_full_history(tmp_path: Path):
    from simpleloop import proposer as prop_mod

    class CapturingAgent:
        prompt = ""

        def run_json(self, prompt, **_kwargs):
            self.prompt = prompt
            return {
                "reflection": "brief",
                "decision": "switch",
                "proposal": "next",
            }

    old_generation = _parallel_history_record(
        3,
        ["old parallel zero", "old parallel one"],
    )
    old_serial = _serial_history_record(8, "old serial proposal")
    recent = [
        _serial_history_record(round_id, f"recent proposal {round_id}")
        for round_id in [20, 30, 40, 50, 60, 70]
    ]
    agent = CapturingAgent()

    prop_mod.propose(
        agent,
        goal="g",
        editable=["src/**"],
        frozen=[],
        history=[old_generation, old_serial, *recent],
        base_sha="accepted-full-sha",
        cwd=tmp_path,
    )

    assert 'proposal_head="old parallel zero"' in agent.prompt
    assert 'proposal_head="old parallel one"' in agent.prompt
    assert 'proposal_head="old serial proposal"' in agent.prompt
    assert 'proposal="old parallel zero"' not in agent.prompt
    assert 'proposal="old serial proposal"' not in agent.prompt
    assert 'proposal="recent proposal 70"' in agent.prompt
```

- [ ] **Step 2: Run the prompt-label test and verify failure**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py::test_proposer_prompt_labels_compact_and_full_history \
  -v
```

Expected: FAIL because `proposer.propose()` currently reads only
`record["proposal"]` or `candidate.get("proposal")`.

- [ ] **Step 3: Implement a shared prompt-field selector**

在 `simpleloop/proposer.py` 的 dataclasses 后加入：

```python
def _proposal_history_field(record: dict) -> tuple[str, str]:
    if "proposal" in record:
        return "proposal", str(record.get("proposal") or "")
    return "proposal_head", str(record.get("proposal_head") or "")
```

在并行 candidate formatter 中先计算：

```python
                    proposal_label, proposal_text = _proposal_history_field(c)
```

并将固定 proposal 片段替换为：

```python
                        f'{proposal_label}="{proposal_text}"'
```

在串行 formatter 中先计算：

```python
                proposal_label, proposal_text = _proposal_history_field(r)
```

并将固定 proposal 片段替换为：

```python
                    f'{proposal_label}="{proposal_text}"'
```

- [ ] **Step 4: Run proposer prompt and parsing tests**

Run:

```bash
python -m pytest simpleloop/tests/test_views_and_parse.py -k proposer -v
```

Expected: all selected tests PASS. Existing single-proposal and batch proposal
contracts remain unchanged.

- [ ] **Step 5: Commit prompt rendering**

```bash
git add simpleloop/proposer.py simpleloop/tests/test_views_and_parse.py
git commit -m "feat: label compact proposals in proposer history"
```

### Task 3: Regression Verification

**Files:**
- Verify: `simpleloop/views.py`
- Verify: `simpleloop/proposer.py`
- Verify: `simpleloop/tests/test_views_and_parse.py`
- Verify: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: the completed Task 1 and Task 2 behavior.
- Produces: evidence that serial history, parallel generation history, resume
  compatibility, proposer parsing, and the full SimpleLoop unit suite still pass.

- [ ] **Step 1: Run the focused context-management tests**

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py \
  -v
```

Expected: all tests PASS.

- [ ] **Step 2: Run the complete SimpleLoop unit suite**

```bash
python -m pytest simpleloop/tests
```

Expected: all tests PASS.

- [ ] **Step 3: Compile the package**

```bash
python -m compileall simpleloop
```

Expected: command exits 0 with no syntax errors.

- [ ] **Step 4: Check formatting and scope**

```bash
git diff --check
git status --short
```

Expected: `git diff --check` exits 0. Status contains only the intended
implementation changes plus any pre-existing parallel-candidate work that was
already present before this plan.

- [ ] **Step 5: Commit any final test-only correction, if one was required**

Skip this step when Step 1 through Step 4 required no edits. Otherwise commit
only the files changed to correct a failing context-management test:

```bash
git add simpleloop/views.py simpleloop/proposer.py \
  simpleloop/tests/test_views_and_parse.py
git commit -m "test: complete proposer context coverage"
```
