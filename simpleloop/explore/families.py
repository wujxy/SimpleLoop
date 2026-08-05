"""Family detection for the Explore module.

A *family* groups findings (and therefore their experiments) by a deterministic
key so that the monitor can detect "every round a fresh Finding, but the same
mechanism in the same code region keeps failing" — the local-exploration trap
that per-finding signals are blind to.

Design notes (see ``docs/explore_refactor_plan.md`` §"Family 识别"):

- **No embedding, no semantic clustering.** The key is built from the
  structured tags Findings already carry.
- **The region bucket is the primary, non-bypassable dimension.** Code regions
  are deterministic file/function paths the Scientist cannot easily rename. The
  mechanism bucket is secondary and display-oriented.
- **Mechanism wording is canonicalized** via token-set Jaccard, so a reworded
  mechanism (``hot-path-micro-optimization`` vs ``hot path micro
  optimization``) collapses to one bucket. This is a blunt lexical guard, not a
  semantic one — deliberately cheap and deterministic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from .classify import jaccard_overlap, tokenize

if TYPE_CHECKING:
    # See classify.py: avoid an import cycle with simpleloop.memory.
    from ..memory.experiment_index import Experiment
    from ..memory.models import Finding


# Two mechanism strings whose token sets overlap at least this much merge into
# one canonical bucket. Deliberately permissive against wording rewrites.
MECHANISM_MERGE_JACCARD = 0.5

UNKNOWN_REGION = "unknown-region"
UNKNOWN_MECHANISM = "unknown-mechanism"


@dataclass(frozen=True)
class FamilyKey:
    """The family grouping key. ``region_bucket`` is the primary stall
    dimension; ``mechanism_bucket`` is the canonicalized display dimension."""

    region_bucket: str
    mechanism_bucket: str

    @property
    def family_id(self) -> str:
        return f"{self.region_bucket}::{self.mechanism_bucket}"


def normalize_region(region: str) -> str:
    """Normalize a code-region tag to a stable bucket.

    Strips a leading ``/`` and any trailing ``:NN`` / ``#LN`` line suffix while
    keeping a ``:FunctionName`` suffix (function-level regions are the unit we
    want to preserve). Returns ``UNKNOWN_REGION`` for empty input.
    """
    if not region:
        return UNKNOWN_REGION
    r = region.strip()
    if r.startswith("/"):
        r = r[1:]
    # Drop a trailing :<digits> line number, but keep :FunctionName.
    r = _strip_line_suffix(r)
    return r or UNKNOWN_REGION


_LINE_SUFFIX_RE = re.compile(r":\d+$")


def _strip_line_suffix(region: str) -> str:
    # Only strip a purely-numeric trailing segment (a line number); an
    # alphabetic/underscore suffix is a function name and is kept.
    m = _LINE_SUFFIX_RE.search(region)
    if m:
        return region[: m.start()]
    return region


def _bucket_path(path: str) -> str:
    """Reduce a changed-path to its file bucket: drop a leading ``/`` and any
    ``:FunctionName`` / ``:NN`` suffix, keeping the file path."""
    if not path:
        return UNKNOWN_REGION
    p = path.strip()
    if p.startswith("/"):
        p = p[1:]
    # Drop everything after the first ':' (changed_paths may carry :Symbol).
    if ":" in p:
        p = p.split(":", 1)[0]
    return p or UNKNOWN_REGION


def region_bucket_for(
    finding: Finding | None,
    finding_experiments: list[Experiment],
) -> str:
    """Derive the region bucket for a finding.

    Order: (1) the most frequent file bucket among the finding's actual
    ``changed_paths`` — deterministic file paths the proposer cannot rename,
    so they are the primary stall dimension; (2) if no experiments yet, the
    first ``code_regions`` tag, normalized; (3) else ``unknown-region``.
    """
    counts: dict[str, int] = {}
    for exp in finding_experiments:
        for path in (exp.changed_paths or ()):
            b = _bucket_path(path)
            if b != UNKNOWN_REGION:
                counts[b] = counts.get(b, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    regions = getattr(finding, "code_regions", ()) or ()
    if regions:
        return normalize_region(regions[0])
    return UNKNOWN_REGION


def canonicalize_mechanisms(
    all_mechanisms: Iterable[str],
) -> dict[str, str]:
    """Map each raw mechanism string to a canonical label.

    Two strings merge into one bucket when their token-set Jaccard is at least
    :data:`MECHANISM_MERGE_JACCARD`. A component's canonical label is its
    lexicographically smallest member. Union-find over the transitive closure,
    so a chain of near-duplicates all collapse together.
    """
    mechs = [m for m in all_mechanisms if m and m.strip()]
    if not mechs:
        return {}
    # Deduplicate exact strings first.
    unique = sorted(set(mechs))
    parent = {m: m for m in unique}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Keep the lexicographically smaller label as the canonical root so the
        # canonical name is stable regardless of insertion order.
        if ra <= rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    for i in range(len(unique)):
        for j in range(i + 1, len(unique)):
            if jaccard_overlap(unique[i], unique[j]) >= MECHANISM_MERGE_JACCARD:
                union(unique[i], unique[j])

    return {m: find(m) for m in mechs}


def mechanism_bucket_for(
    finding: Finding | None,
    canonical: dict[str, str],
) -> str:
    """Derive the mechanism bucket for a finding.

    Uses the canonical label of the finding's first mechanism (after sorting,
    so the dominant mechanism is stable). Empty → ``unknown-mechanism``.
    """
    mechs = getattr(finding, "mechanisms", ()) or ()
    if not mechs:
        return UNKNOWN_MECHANISM
    ordered = sorted(m for m in mechs if m and m.strip())
    if not ordered:
        return UNKNOWN_MECHANISM
    return canonical.get(ordered[0], ordered[0])


def assign_families(
    findings: dict[str, Finding],
    experiments: list[Experiment],
) -> dict[str, FamilyKey]:
    """Map each finding id to its :class:`FamilyKey`.

    Experiments whose ``finding_id`` is ``None`` or unknown are *not* assigned a
    family here (they are still counted in the global view). Mechanisms are
    canonicalized across *all* findings up front so cross-finding rewording
    collapses to a single bucket.
    """
    # Collect every mechanism string once for canonicalization.
    all_mechs: list[str] = []
    for f in findings.values():
        all_mechs.extend(f.mechanisms or ())
    canonical = canonicalize_mechanisms(all_mechs)

    # Bucket experiments by finding for the region fallback.
    by_finding: dict[str, list[Experiment]] = {}
    for exp in experiments:
        fid = exp.finding_id
        if fid:
            by_finding.setdefault(fid, []).append(exp)

    out: dict[str, FamilyKey] = {}
    for fid, finding in findings.items():
        region = region_bucket_for(finding, by_finding.get(fid, []))
        mechanism = mechanism_bucket_for(finding, canonical)
        out[fid] = FamilyKey(region_bucket=region, mechanism_bucket=mechanism)
    return out
