"""The proposer (Scientist): a standalone, relocatable research agent.

This package is the "self" that SimpleLoop's Host runs as a subprocess
(``python -m simpleloop.proposer_lane_worker``) and — in the RSI design — the
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
