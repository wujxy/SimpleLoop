#!/usr/bin/env python
"""Drift check: compare count_pairs output against a frozen baseline contract.

This is the "numerical equivalence" gate. A correct optimization must produce
exactly these counts on exactly these inputs. Writes benchmarks/drift.csv and
exits non-zero on any mismatch.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from tinyalgo import count_pairs

# Frozen baseline: (points, radius) -> expected count.
# These are deterministic; any drift means the optimizer broke correctness.
BASELINE = [
    (([(0, 0), (1, 1), (2, 2), (0, 2), (2, 0)], 2), 8),
    (([(x, y) for x in range(4) for y in range(4)], 3), 90),
    (([(i, -i) for i in range(10)], 4), 17),
    (([(0, 0), (1, 0), (0, 1), (1, 1)], 1), 4),
    (([(5, 5), (5, 8), (8, 5), (8, 8), (6, 6)], 3), 7),
]


def main() -> int:
    out_dir = Path("benchmarks")
    out_dir.mkdir(exist_ok=True)
    mismatches: list[str] = []
    rows = []
    for idx, ((points, radius), expected) in enumerate(BASELINE):
        got = count_pairs(points, radius)
        ok = got == expected
        rows.append({"case": idx, "radius": radius, "n": len(points),
                     "expected": expected, "got": got, "ok": ok})
        if not ok:
            mismatches.append(f"case {idx}: expected {expected}, got {got}")
    with (out_dir / "drift.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["case", "radius", "n", "expected", "got", "ok"])
        w.writeheader()
        w.writerows(rows)
    if mismatches:
        print("DRIFT FAILED:")
        for m in mismatches:
            print(f"  {m}")
        return 1
    print(f"drift OK: {len(BASELINE)} cases match baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
