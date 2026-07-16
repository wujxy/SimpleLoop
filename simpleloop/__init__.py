"""SimpleLoop: a minimal serial LLM optimization loop.

proposer -> executor -> judger, one of each per round, no batch, no parallel,
no early stop. The orchestrator does only scheduling; the three roles do the
thinking and the harness does the deterministic work (commit, eval, diff).
"""
