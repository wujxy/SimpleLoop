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

# The Host↔proposer contract version this self speaks. The Host validates it at
# viability time (`proposer_lane_worker --check --contract-version …`): a
# self-change that needs a NEW contract must bump this, and that candidate then
# FAILS viability, because the Host only speaks one version (contract §11 —
# changing the contract is a Kernel change, not a natural side-effect of
# self-modification). The Host's expected value lives in
# ``simpleloop.self_repo.EXPECTED_CONTRACT_VERSION``; this declaration is the
# self's side of the agreement.
CONTRACT_VERSION = "proposer-cli-v0"

