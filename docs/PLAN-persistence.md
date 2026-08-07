# Plan: Handoff Information Persistence

## Problem

Key handoff artifacts are only persisted at round-end to `history.jsonl`, or
not at all. During a round (which can take 30+ minutes with parallel executors),
nothing is on disk. If the process crashes, you lose:

- The proposals the proposer generated (only in memory until `history.jsonl`)
- The executor's full output (discarded at `executor.py:54` — `run_text` return
  value is thrown away)
- What the executor actually did (only the commit SHA survives, in git)
- Per-branch proposer traces (only the combined summary in `proposer_traces/`)

The user wants to trace/reproduce any stage of the pipeline at any time, even
mid-round.

## Design

Add a `handoffs/` directory under the run dir. Each pipeline stage writes a
JSON file atomically the moment it completes — not at round-end. Files are
named by stage and candidate:

```
runs/<run>/
  handoffs/
    r0/
      proposals.json          # proposer output (all proposals + findings)
      r0-c0.executor.json     # executor output (prompt, response, changed_paths, sha)
      r0-c0.eval.json         # eval output (metrics, gates, eval_block)
      r0-c1.executor.json
      r0-c1.eval.json
      ...
```

Each file is written atomically (tmp + `os.replace`) the moment that stage
finishes. They are non-authoritative — `history.jsonl` remains the source of
truth. The handoff files are for observability/reproduction only.

## Steps

### Step 1: Persist proposals immediately after proposer finishes

In `loop.py`, right after `_write_proposer_trace()` (line 308), write
`handoffs/r{N}/proposals.json` containing:
- `round_id`, `parent_sha`, `timestamp`
- For each proposal: `instruction`, `finding_id`, `evidence_refs`,
  `material_difference`
- The proposer trace (branches with signatures + instructions)
- `abstained`, `abstain_reason` if applicable

This is the single most important file — it captures what the proposer decided
before any executor starts.

### Step 2: Capture and persist executor output

In `candidate_worker.py:run_candidate()`, after `executor_mod.execute()`
returns (line 167), write `handoffs/r{N}/r{N}-c{C}.executor.json` containing:
- `candidate_id`, `proposal` (the instruction text), `parent_sha`
- `sha` (commit SHA, or null), `changed_paths`
- `status` (COMMITTED / NO_CHANGE / PATH_GATE_REJECTED / EXECUTOR_FAILED)
- `executor_response` — the agent's text output (currently discarded)
- `elapsed_seconds`

This requires `execute()` to return the agent's text response alongside the
`ExecResult`. Add an `output: str` field to `ExecResult`.

### Step 3: Persist eval output

In `candidate_worker.py:run_candidate()`, after `evals.run_eval()` returns
(line 207), write `handoffs/r{N}/r{N}-c{C}.eval.json` containing:
- `candidate_id`, `sha`
- `metrics`, `gates`, `gate_passed`, `eligible`
- `eval_block` (the full eval output, uncapped — `history.jsonl` caps at 6000)
- `returncodes`, `commands_ok`

### Step 4: Persist executor failure info

In `candidate_worker.py:run_candidate()`, the `except (AgentError, ValueError)`
block (line 168), write `handoffs/r{N}/r{N}-c{C}.executor.json` with the
failure info:
- `status: "EXECUTOR_FAILED"`, `error: str(exc)`
- The proposal text (so you can reproduce the executor call)

### Step 5: Write a helper module for atomic handoff writes

Create `simpleloop/harness/handoff.py` with:
```python
def write_handoff(run_dir: Path, round_id: int, name: str, data: dict) -> None:
    """Atomically write a handoff JSON file."""
    d = run_dir / "handoffs" / f"r{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
```

All steps use this helper. Centralizes the atomic-write pattern and the
directory layout.

## What is NOT changed

- `history.jsonl` remains the authoritative ledger, written at round-end.
- `proposer_traces/` remains as-is (non-authoritative behavioral telemetry).
- HEPJob backend's `inflight_round.json` and `rounds/` manifests are unchanged
  (they already persist intermediate state).
- The handoff files are additive — no existing file is modified or removed.
- No change to the proposer's in-memory conversation history (that would be a
  much larger change and is less useful than the handoff artifacts).

## Verification

- After proposer finishes: `handoffs/r0/proposals.json` exists with all proposals
- After each executor finishes: `handoffs/r0/r0-cN.executor.json` exists
- After each eval finishes: `handoffs/r0/r0-cN.eval.json` exists
- Files are valid JSON, readable mid-round
- `history.jsonl` content is unchanged (same round records)
- All existing tests pass
