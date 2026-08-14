"""The proposer (Scientist): a standalone, relocatable research agent.

This package is the "self" that the Host loads only inside a unified worker
subprocess and — in the RSI design — the
unit that gets snapshotted into a run-local self-repo and self-modified. It
owns the Scientist control loop, its research tools, its model client, and its
own research memory (findings/experiments/retrieval). The Host owns only the
evaluator, gates, and the authoritative task ledger (``history.jsonl``), which
this package reads.

Fully standalone: zero ``simpleloop`` imports. The shared infrastructure it
once depended on (container.runtime, processes, harness.memory) is vendored
into this package as ``runtime.py``, ``child_processes.py``, and
``memory/history.py`` — so a snapshot of ``proposer/`` into
``run_dir/self/repo/`` is self-sufficient and is what the run executes.
"""

# A self-declared version string. Purely informational — emitted in self-review
# results for observability and free for the self to change. Nothing enforces it:
# viability is a behavior-level smoke test (can the self still emit a proposal?),
# NOT a contract-version gate, so the self may evolve its own protocol freely as
# long as it keeps functioning in the loop.
CONTRACT_VERSION = "proposer-cli-v0"
