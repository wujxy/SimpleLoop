"""The three LLM roles (proposer, executor, judger) and their claude adapter.

Roles think; they never own deterministic ground truth. Commits, eval
execution, metric parsing, gating, and best selection live in
simpleloop.harness.
"""
