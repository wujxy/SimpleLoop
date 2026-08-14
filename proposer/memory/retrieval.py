"""Diversity-aware retrieval over the Experiment Ledger and Findings.

Two search entry points (search_experiments, search_findings) share a small
BM25 core (Robertson's classic formula) and a lightweight MMR reranker so
retrieved evidence is scored by relevance without collapsing into
near-duplicates. Design doc §6 spells out the intent; this file is the
concrete Phase 2 implementation.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .experiment_index import Experiment
from .models import Finding


_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+")


def tokenize(text: str) -> list[str]:
    """Lowercased word-ish tokens. Keeps dots and slashes so ``QPDF.cc`` and
    ``OMILREC/src/foo.cc`` survive as searchable atoms."""
    return [tok.lower() for tok in _TOKEN_RE.findall(text or "")]


# --- BM25 ------------------------------------------------------------------

_BM25_K1 = 1.5
_BM25_B = 0.75


class BM25Index:
    """Minimal BM25 over a corpus of token lists."""

    def __init__(self, docs: list[list[str]]):
        self.docs = [tuple(toks) for toks in docs]
        self.doc_lens = [len(doc) for doc in self.docs]
        self.avgdl = (
            sum(self.doc_lens) / len(self.doc_lens) if self.doc_lens else 0.0
        )
        self._df: dict[str, int] = {}
        for doc in self.docs:
            for tok in set(doc):
                self._df[tok] = self._df.get(tok, 0) + 1
        self._n = len(self.docs)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1 + (self._n - df + 0.5) / (df + 0.5))

    def score(self, query: list[str], doc_idx: int) -> float:
        if not self.docs or self.avgdl == 0:
            return 0.0
        doc = self.docs[doc_idx]
        dl = self.doc_lens[doc_idx]
        # Term frequency in the doc.
        tf: dict[str, int] = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1
        score = 0.0
        for term in query:
            if term not in tf:
                continue
            idf = self._idf(term)
            freq = tf[term]
            denom = freq + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * dl / self.avgdl
            )
            score += idf * freq * (_BM25_K1 + 1) / denom
        return score

    def rank(self, query: list[str]) -> list[tuple[int, float]]:
        scored = [
            (i, self.score(query, i))
            for i in range(len(self.docs))
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored


# --- MMR -------------------------------------------------------------------

def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def mmr_rerank(
    ranked: list[tuple[int, float]],
    doc_tokens: list[list[str]],
    *,
    limit: int,
    lambda_: float = 0.5,
) -> list[tuple[int, float]]:
    """Maximal Marginal Relevance: greedy pick from ``ranked`` maximizing
    ``λ·rel - (1-λ)·max_sim_to_already_picked``. Similarity is Jaccard over
    token sets, which is cheap and needs no vector model."""
    if not ranked:
        return []
    doc_sets = [set(tokens) for tokens in doc_tokens]
    max_rel = max((score for _, score in ranked), default=0.0) or 1.0
    picked: list[tuple[int, float]] = []
    remaining = list(ranked)
    while remaining and len(picked) < limit:
        best_idx = -1
        best_score = float("-inf")
        for pos, (doc_idx, rel) in enumerate(remaining):
            rel_norm = rel / max_rel
            if not picked:
                penalty = 0.0
            else:
                penalty = max(
                    _jaccard(doc_sets[doc_idx], doc_sets[chosen])
                    for chosen, _ in picked
                )
            score = lambda_ * rel_norm - (1 - lambda_) * penalty
            if score > best_score:
                best_score = score
                best_idx = pos
        chosen = remaining.pop(best_idx)
        picked.append(chosen)
    return picked


# --- Experiment search -----------------------------------------------------

_EVAL_SNIPPET_CHARS = 500


def _experiment_tokens(exp: Experiment) -> list[str]:
    parts = [
        exp.proposal,
        " ".join(exp.changed_paths),
        exp.eval_block[:_EVAL_SNIPPET_CHARS],
        exp.status,
    ]
    return tokenize(" ".join(parts))


def rank_experiments(
    experiments: list[Experiment],
    *,
    query: str,
    limit: int,
    mmr_lambda: float = 0.5,
) -> list[tuple[Experiment, float]]:
    """BM25 rank + MMR rerank. Returns up to ``limit`` (experiment, score)
    pairs, best first. Empty experiments -> []."""
    if not experiments:
        return []
    doc_tokens = [_experiment_tokens(exp) for exp in experiments]
    query_tokens = tokenize(query)
    if not query_tokens:
        # No signal in the query: preserve deterministic order (round, cand).
        indexed = sorted(
            range(len(experiments)),
            key=lambda i: (experiments[i].round, experiments[i].candidate),
        )
        return [(experiments[i], 0.0) for i in indexed[:limit]]
    index = BM25Index(doc_tokens)
    ranked = [pair for pair in index.rank(query_tokens) if pair[1] > 0]
    reranked = mmr_rerank(
        ranked, doc_tokens, limit=limit, lambda_=mmr_lambda,
    )
    return [(experiments[idx], score) for idx, score in reranked]


def diverse_experiment_search(
    experiments: list[Experiment],
    *,
    query: str,
    relevant: int = 3,
    contrasting: int = 2,
    diverse: int = 2,
    mmr_lambda: float = 0.5,
) -> dict[str, list[tuple[Experiment, float]]]:
    """Design doc §6.3 three-bucket retrieval.

    - relevant: top BM25+MMR hits.
    - contrasting: same changed-path family as the top hit, but flipped
      ``gate_passed``.
    - diverse: further MMR picks constrained to code regions not represented
      in ``relevant``.
    """
    if not experiments:
        return {"relevant": [], "contrasting": [], "diverse": []}
    ranked = rank_experiments(
        experiments, query=query, limit=max(1, relevant),
        mmr_lambda=mmr_lambda,
    )
    relevant_hits = ranked[:relevant]
    relevant_ids = {hit.experiment_id for hit, _ in relevant_hits}

    # --- contrasting: same top region, opposite gate_passed ---------------
    contrasting_hits: list[tuple[Experiment, float]] = []
    if relevant_hits and contrasting > 0:
        anchor = relevant_hits[0][0]
        anchor_prefix = _top_region_prefix(anchor)
        anchor_gate = anchor.gate_passed
        candidates = [
            exp for exp in experiments
            if exp.experiment_id not in relevant_ids
            and exp.gate_passed != anchor_gate
            and (
                anchor_prefix is None
                or any(
                    path.startswith(anchor_prefix)
                    for path in exp.changed_paths
                )
            )
        ]
        if candidates:
            sub = rank_experiments(
                candidates, query=query, limit=contrasting,
                mmr_lambda=mmr_lambda,
            )
            contrasting_hits = sub
            for hit, _ in sub:
                relevant_ids.add(hit.experiment_id)

    # --- diverse: from code regions not yet covered -----------------------
    diverse_hits: list[tuple[Experiment, float]] = []
    if diverse > 0:
        covered_prefixes = {
            _top_region_prefix(hit) for hit, _ in relevant_hits
        }
        covered_prefixes.discard(None)
        candidates = [
            exp for exp in experiments
            if exp.experiment_id not in relevant_ids
            and _top_region_prefix(exp) not in covered_prefixes
        ]
        if candidates:
            sub = rank_experiments(
                candidates, query=query, limit=diverse,
                mmr_lambda=mmr_lambda,
            )
            if not sub:
                # Diversity trumps BM25 relevance here: even a zero-score
                # region we haven't touched is worth surfacing.
                sub = [
                    (exp, 0.0)
                    for exp in candidates[:diverse]
                ]
            diverse_hits = sub

    return {
        "relevant": relevant_hits,
        "contrasting": contrasting_hits,
        "diverse": diverse_hits,
    }


def _top_region_prefix(exp: Experiment) -> str | None:
    """First two path segments of the first changed file: a coarse "code
    region" bucket. ``OMILRECV2/src/OMILREC.cc`` -> ``OMILRECV2/src``."""
    if not exp.changed_paths:
        return None
    path = exp.changed_paths[0]
    parts = path.split("/")
    if len(parts) <= 2:
        return path
    return "/".join(parts[:2])


# --- Finding search --------------------------------------------------------

def _finding_tokens(finding: Finding) -> list[str]:
    parts = [
        finding.question,
        " ".join(finding.mechanisms),
        " ".join(finding.code_regions),
    ]
    return tokenize(" ".join(parts))


def rank_findings(
    findings: list[Finding],
    *,
    query: str,
    limit: int,
    mmr_lambda: float = 0.5,
) -> list[tuple[Finding, float]]:
    if not findings:
        return []
    doc_tokens = [_finding_tokens(f) for f in findings]
    query_tokens = tokenize(query)
    if not query_tokens:
        return [(f, 0.0) for f in findings[:limit]]
    index = BM25Index(doc_tokens)
    ranked = [pair for pair in index.rank(query_tokens) if pair[1] > 0]
    reranked = mmr_rerank(
        ranked, doc_tokens, limit=limit, lambda_=mmr_lambda,
    )
    return [(findings[idx], score) for idx, score in reranked]
