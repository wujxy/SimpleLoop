"""The proposer (Scientist): a standalone, relocatable research agent.

This package is the "self" that SimpleLoop's Host runs as a subprocess
(``python -m simpleloop.proposer_lane_worker``) and — in the RSI design — the
unit that gets snapshotted into a run-local self-repo and self-modified. It
owns the Scientist control loop, its research tools, its model client, and its
own research memory (findings/experiments/retrieval). The Host owns only the
evaluator, gates, and the authoritative task ledger (``history.jsonl``), which
this package reads.

S2b(i): logically separated (this top-level package still imports a small
shared-infrastructure subset of ``simpleloop`` — container.runtime,
processes, harness.memory — which S2b(ii) will vendor to make the package
fully standalone).
"""
