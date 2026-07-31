#!/usr/bin/env python
"""Benchmark: measure median ms/call for count_pairs on a fixed input.

Writes benchmarks/speed.csv with the measured ms_per_call. This is the speed
metric the harness parses from eval output. Correctness is NOT checked here —
that's check_drift.py's job.
"""
from __future__ import annotations

import csv
import statistics
import time
from pathlib import Path

from tinyalgo import count_pairs


def _build_input(n: int = 200) -> list[tuple[int, int]]:
    # deterministic, no randomness (Math.random forbidden in harness too)
    return [((i * 7) % 50, (i * 13) % 50) for i in range(n)]


def main() -> int:
    pts = _build_input(200)
    radius = 5
    # warmup
    count_pairs(pts, radius)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        count_pairs(pts, radius)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000.0)
    ms = statistics.median(times)
    out_dir = Path("benchmarks")
    out_dir.mkdir(exist_ok=True)
    with (out_dir / "speed.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value"])
        w.writeheader()
        w.writerow({"metric": "ms_per_call", "value": f"{ms:.4f}"})
    print(f"ms_per_call={ms:.4f}  (n={len(pts)}, radius={radius})")
    return 0


if __name__ == "__main__":
    main()
