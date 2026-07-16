"""tinyalgo: a deliberately-slow but correct pair-counting implementation.

The pair-count problem: given a list of N points (each a 2-tuple of ints), count
the number of unordered pairs (i, j) whose Manhattan distance is <= R.

This reference is correct but intentionally slow: it recomputes the distance with
redundant abs() calls and uses a plain Python double loop. It is the optimization
target for the SimpleLoop tiny example.
"""
from __future__ import annotations


def manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Manhattan distance between two 2-D integer points."""
    dx = a[0] - b[0]
    if dx < 0:
        dx = -dx
    dy = a[1] - b[1]
    if dy < 0:
        dy = -dy
    return dx + dy


def count_pairs(points: list[tuple[int, int]], radius: int) -> int:
    """Count unordered pairs (i<j) whose Manhattan distance is <= radius.

    Correct but slow: O(N^2) with redundant work in the inner test.
    """
    n = len(points)
    total = 0
    for i in range(n):
        for j in range(i + 1, n):
            # redundant: recompute both coordinates' deltas separately
            d = manhattan(points[i], points[j])
            if d <= radius:
                total = total + 1
    return total
