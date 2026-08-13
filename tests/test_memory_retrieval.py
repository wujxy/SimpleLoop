"""Tests for BM25 + MMR retrieval and three-bucket search."""
from __future__ import annotations

from proposer.memory.experiment_index import Experiment
from proposer.memory.models import Finding
from proposer.memory.retrieval import (
    BM25Index,
    diverse_experiment_search,
    mmr_rerank,
    rank_experiments,
    rank_findings,
    tokenize,
)


def _exp(eid: str, *, proposal: str = "", paths=("src/a.cc",),
         gate: bool = True, fid: str | None = None) -> Experiment:
    return Experiment(
        experiment_id=eid, round=int(eid[1:eid.index("c")]),
        candidate=int(eid[eid.index("c") + 1:]),
        proposal=proposal, parent_sha="p", candidate_sha=None,
        status="COMPLETED", gate_passed=gate, eligible=gate,
        selected=False, metrics={}, changed_paths=tuple(paths),
        finding_id=fid, eval_block="",
    )


def _finding(fid: str, *, question: str, mechanisms=(), code_regions=()) -> Finding:
    return Finding(
        id=fid, question=question,
        mechanisms=tuple(mechanisms), code_regions=tuple(code_regions),
        state="active", created_round=0, last_touched_round=0,
        experiment_refs=(), parent_finding_id=None, stats={},
    )


def test_tokenize_keeps_slashes_and_dots():
    tokens = tokenize("OMILREC/src/QPDF.cc uses cache_lookup")
    assert "omilrec/src/qpdf.cc" in tokens
    assert "cache_lookup" in tokens


def test_bm25_ranks_matching_documents_higher():
    docs = [
        tokenize("hoist QPDF cache lookup outside the event loop"),
        tokenize("rewrite JSON parser for Config"),
        tokenize("cache lookup for QPDF at initialization"),
    ]
    index = BM25Index(docs)
    ranking = [i for i, _ in index.rank(tokenize("QPDF cache lookup"))]
    assert ranking[0] in {0, 2}
    assert ranking[-1] == 1


def test_mmr_rerank_penalizes_near_duplicates():
    docs = [
        tokenize("cache lookup QPDF hoist"),
        tokenize("cache lookup QPDF hoist"),          # near-duplicate
        tokenize("rewrite JSON parser for Config"),
    ]
    ranked = [(0, 3.0), (1, 2.9), (2, 0.5)]
    picked = mmr_rerank(ranked, docs, limit=2, lambda_=0.4)
    ids = [i for i, _ in picked]
    assert ids[0] == 0
    # MMR should prefer the diverse doc over the near-duplicate.
    assert ids[1] == 2


def test_rank_experiments_returns_top_matches():
    experiments = [
        _exp("r0c0", proposal="hoist QPDF cache lookup"),
        _exp("r1c0", proposal="rewrite JSON parser"),
        _exp("r2c0", proposal="pack values for QPDF layout"),
    ]
    hits = rank_experiments(experiments, query="QPDF cache", limit=2)
    assert {e.experiment_id for e, _ in hits} <= {"r0c0", "r2c0"}


def test_rank_experiments_empty_query_returns_deterministic_order():
    experiments = [_exp("r0c0"), _exp("r1c0")]
    hits = rank_experiments(experiments, query="", limit=5)
    assert [e.experiment_id for e, _ in hits] == ["r0c0", "r1c0"]


def test_diverse_experiment_search_produces_three_buckets():
    experiments = [
        _exp("r0c0", proposal="hoist QPDF cache lookup",
             paths=("OMILREC/src/QPDF.cc",), gate=True),
        _exp("r1c0", proposal="hoist QPDF cache lookup differently",
             paths=("OMILREC/src/QPDF.cc",), gate=False),  # contrasting
        _exp("r2c0", proposal="rewrite JSON parser for Config",
             paths=("Config/src/JSON.cc",), gate=True),      # diverse region
        _exp("r3c0", proposal="rewrite pack values for QPDF layout",
             paths=("OMILREC/src/QPDF.cc",), gate=True),
    ]
    # relevant=1 keeps r0c0 as anchor; contrasting then surfaces r1c0
    # (same top region, opposite gate); diverse comes from a different region.
    buckets = diverse_experiment_search(
        experiments, query="QPDF cache", relevant=1, contrasting=1, diverse=1,
    )
    rel_ids = {e.experiment_id for e, _ in buckets["relevant"]}
    con_ids = {e.experiment_id for e, _ in buckets["contrasting"]}
    div_ids = {e.experiment_id for e, _ in buckets["diverse"]}
    assert rel_ids == {"r0c0"}
    assert con_ids == {"r1c0"}
    assert div_ids == {"r2c0"}


def test_rank_findings_uses_finding_text():
    findings = [
        _finding("F-001", question="cache lookup in QPDF hot loop",
                 mechanisms=("hoist", "cache")),
        _finding("F-002", question="rewrite JSON parser",
                 mechanisms=("parser",)),
    ]
    hits = rank_findings(findings, query="QPDF cache", limit=1)
    assert hits[0][0].id == "F-001"
