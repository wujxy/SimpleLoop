# tinyalgo optimization target

A deliberately-slow-but-correct 2-D Manhattan pair-counting implementation, plus
correctness tests, a drift check, and a speed benchmark. It is the optimization
target for the SimpleLoop `tiny_algo_opt` example.

## Layout
- `tinyalgo/__init__.py` — `count_pairs(points, radius)` (slow reference)
- `tests/test_correctness.py` — pins exact expected counts
- `scripts/check_drift.py` — numerical-equivalence gate vs a frozen baseline
- `scripts/bench.py` — measures `ms_per_call`, writes `benchmarks/speed.csv`

## Run the checks
```bash
cd repo
PYTHONPATH=. python -m pytest tests/ -q
PYTHONPATH=. python scripts/check_drift.py
PYTHONPATH=. python scripts/bench.py
```
