"""Correctness tests for tinyalgo.count_pairs.

A correct optimization MUST keep all of these passing. They pin the exact
expected counts for several inputs, so any change to the algorithm that alters
results will fail here.
"""
import pytest

from tinyalgo import count_pairs


def test_empty():
    assert count_pairs([], 5) == 0


def test_single_point():
    assert count_pairs([(0, 0)], 5) == 0


def test_two_within_radius():
    assert count_pairs([(0, 0), (1, 1)], 2) == 1


def test_two_outside_radius():
    assert count_pairs([(0, 0), (5, 5)], 2) == 0


def test_boundary_exactly_radius():
    # distance exactly == radius counts as within (<=)
    assert count_pairs([(0, 0), (3, 0)], 3) == 1


def test_known_grid():
    # 3x3 grid, radius 2: count pairs with manhattan dist <= 2
    pts = [(x, y) for x in range(3) for y in range(3)]
    assert count_pairs(pts, 2) == 26


def test_diamond_cluster():
    pts = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]
    # all pairwise distances <= 2 -> C(5,2) = 10
    assert count_pairs(pts, 2) == 10


def test_large_radius_all_pairs():
    import math
    n = 20
    pts = [(i, i) for i in range(n)]
    assert count_pairs(pts, 10**9) == n * (n - 1) // 2


def test_negative_coords():
    assert count_pairs([(-2, -2), (0, 0), (2, 2)], 4) == 2
