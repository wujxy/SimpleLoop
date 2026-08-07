"""Hypothesis card: a lightweight, evidence-free research direction.

The Generator produces these in bulk (G1-G9 as entry-point angles). They are
NOT proposals — they are unverified leads that a branch researcher investigates
deeply; if the mechanism doesn't exist in the code, the branch abandons
(producing a finding for Explore). See PLAN.md for the full architecture.

The structural signature (region x mechanism x intervention_family) is the
dedup key for the diversity archive: two cards with the same signature compete
for one niche slot, so the portfolio cannot fill with near-duplicate variants
of the same idea.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..explore.families import normalize_region, _bucket_path


UNKNOWN = "unknown"


@dataclass(frozen=True)
class HypothesisCard:
    """One lightweight research direction from a generative operation.

    ``generative_op`` is the G1-G9 label that produced this card (for
    traceability and frame-free accounting). ``region`` is a code-region hint
    (file path or subsystem). ``mechanism`` is the suspected cost or limitation.
    ``intervention_family`` is the structural direction of the fix. All three
    are free-form strings; the signature canonicalizes them.
    """

    generative_op: str
    region: str
    mechanism: str
    intervention_family: str
    why_plausible: str
    critical_unknown: str
    # Factual observations the Generator read from the source (NOT code
    # snippets). The hypothesis must follow from these facts. Carried to the
    # cognitive partner so it can audit the Generator's factual basis.
    facts_read: tuple[str, ...] = ()
    # Slot kind: "guided" (shaped by Explore boundary) or "free" (frame-free).
    slot: str = "guided"

    def signature(self) -> tuple[str, str, str]:
        """Canonical (region, mechanism, intervention) bucket for dedup.

        Region is file-bucketed (drops :FunctionName), mechanism and
        intervention are whitespace-normalized lowercase — good enough to
        collapse "cache repeated lookups" / "caching repeated lookups" without
        over-collapsing genuinely different mechanisms.
        """
        return (
            _bucket_path(self.region) if "/" in self.region or "." in self.region
            else normalize_region(self.region),
            _canon(self.mechanism),
            _canon(self.intervention_family),
        )


def _canon(text: str) -> str:
    """Whitespace-normalized lowercase — the cheap canonical form for
    free-form mechanism/intervention strings. We do NOT use the Jaccard
    merge from canonicalize_mechanisms here because that needs the full
    population to build the union-find; for card dedup a simple normalize
    is enough (the Generator rarely produces near-spelling variants)."""
    return " ".join(text.lower().split()) or UNKNOWN


def dedup_by_signature(
    cards: list[HypothesisCard], *, per_bin: int = 1,
) -> list[HypothesisCard]:
    """Keep at most ``per_bin`` cards per structural signature.

    When multiple cards share a signature, rotate which one is kept by index
    (deterministic, no Math.random) so the first niche is not always preferred.
    Returns cards in signature-stable order (first occurrence of each unique
    signature), preserving the generator's ordering across niches.
    """
    bins: dict[tuple[str, str, str], list[HypothesisCard]] = {}
    order: list[tuple[str, str, str]] = []
    for card in cards:
        sig = card.signature()
        if sig not in bins:
            bins[sig] = []
            order.append(sig)
        bins[sig].append(card)
    out: list[HypothesisCard] = []
    for sig in order:
        bucket = bins[sig]
        # Rotate: keep per_bin cards, starting from a deterministic offset
        # based on the number of bins seen so far (avoids always taking [0]).
        offset = len(out) % len(bucket) if bucket else 0
        for i in range(min(per_bin, len(bucket))):
            out.append(bucket[(offset + i) % len(bucket)])
    return out


def distinct_niches(cards: list[HypothesisCard]) -> int:
    """Count unique signatures — the true diversity of a card set."""
    return len({c.signature() for c in cards})
