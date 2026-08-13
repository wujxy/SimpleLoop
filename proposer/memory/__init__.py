"""Persistent research memory: Experiment Ledger + Active Findings + Retrieval.

Replaces the old `candidate note / round annotation / full-history ref:note
directory / backfill_notes` mechanism. See
docs/simpleloop_research_history_memory_redesign.md.
"""
from __future__ import annotations

from .models import (
    ExistingFindingTarget,
    Finding,
    NewFindingTarget,
    ResearchProposal,
)
from .service import MemoryService

__all__ = (
    "ExistingFindingTarget",
    "Finding",
    "MemoryService",
    "NewFindingTarget",
    "ResearchProposal",
)
