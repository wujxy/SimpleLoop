"""Deterministic harness components: everything an LLM must not be trusted with.

Eval execution + metric parsing (evals), the frozen/editable diff gate (gate),
git isolation and harness-owned commits (workspace), the JSONL history and
best selection (store), episode refs + insights (memory), and per-role history
projections (views).
"""
