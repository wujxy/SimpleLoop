"""Pure generative operators used by Scientist EXPLORE mode."""
from __future__ import annotations


GENERATIVE_OPS = tuple(f"G{i}" for i in range(1, 10))

_DEFINITIONS = {
    "G1": "Cross-domain isomorphic transfer: find a similar relational structure in another region/domain and map its solution here.",
    "G2": "Decompose and recompose: split along data-flow / abstraction / time-phase axes; recombine into a different structure.",
    "G3": "Idealize and take limit: push a resource to an extreme (compute free, memory infinite, events → ∞) and keep what survives.",
    "G4": "Symmetry lift: find broken/ignored symmetries; unify special cases or deliberately break one for this workload.",
    "G5": "Invert: don't accelerate, eliminate; don't compute now, precompute or lazily compute; don't store results, store regenerators.",
    "G6": "Algorithm/representation/paradigm sweep: treat the current implementation as one choice among many — other algorithm families, data structures, layouts, indexing, sparse/dense, offline/online.",
    "G7": "Anomaly amplification: treat failures/regressions/no-gain as entrances — if it isn't noise, what hidden term is it revealing?",
    "G8": "Form first, explanation later: find an interesting structure (recurring pattern, transform, geometric shape in the call graph) and let it suggest ideas.",
    "G9": "Dimension, scale, and growth: vary scale variables (events, hits, objects, table size, parallelism). Which cost grows fastest? Is there a threshold past which a different structure is optimal?",
}


def g_definition(op: str) -> str:
    """Return one named operator definition, rejecting invented operators."""
    try:
        return f"{op} — {_DEFINITIONS[op]}"
    except KeyError as exc:
        raise ValueError(f"unknown generative operator: {op}") from exc


def render_generative_basis(ops: tuple[str, ...]) -> str:
    if not ops:
        return ""
    return "## Assigned Generative Basis\n\n" + "\n\n".join(
        g_definition(op) for op in ops
    )
