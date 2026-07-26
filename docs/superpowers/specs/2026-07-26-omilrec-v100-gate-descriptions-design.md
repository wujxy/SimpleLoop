# OMILREC v1.0.0 task gate descriptions

## Scope

Update the prose in all three active task configurations under
`examples/omilrec-v100-opt/`:

- `task.yaml`
- `task_hints.yaml`
- `task_nohints.yaml`

The evaluator commands, metric keys, numerical thresholds, editable paths,
baseline reference, and loop settings do not change.

## Shared factual core

All three configurations must state the same facts:

- The objective is to reduce probe-free `SPEED_MS`.
- OMILREC v1.0.0 is the sole source of algorithm and numerical truth.
- The proposer may discover new code-grounded optimization directions; listed
  techniques are suggestions, not a closed search space.
- A candidate is usable only when `CONTRACT`, `FCN`, `CONSISTENCY`, and
  `EVAL_RESULT` all pass.
- The gate, fixtures, reference data, thresholds, evaluator, and build wiring
  must not be weakened, regenerated, bypassed, or replaced.

The text must not mention obsolete version labels, an old replay pack, deleted
likelihood files, or any second implementation or version as a truth source.

## Gate descriptions

### CONTRACT

Explain that this gate protects the measuring system itself: the FCN test
remains linked to the production member, the frozen assets and thresholds
remain intact, and the complete evaluator structure remains present. Modifying,
removing, reducing, bypassing, or replacing those checks, including adding a
second likelihood implementation for the test, fails the contract.

### FCN

Explain that the real production
`OMILRECV2::Calculate_EVLikelihood` is evaluated at four stages for four fixed
events. All 16 results must be finite and unique, and each must remain below
`1e-13` relative error against frozen v1.0.0 truth.

Call out representative risky changes without presenting an exhaustive ban
list: reassociating or reordering reductions, changing floating-point types or
casts, replacing mathematical expressions with nominally equivalent forms, or
folding constants can alter rounding and fail this gate. Fetch, layout, cache,
and invariant-work optimizations remain possible when they preserve the exact
tested results.

### CONSISTENCY

Explain that all 18 reconstructed events must be finite and remain within the
v1.0.0 end-to-end limits: Euclidean 3D position at most 4 mm, energy at most
7 keV, t0 at most 10 ps, and peSum at most 0.1 PE. Arithmetic or structural
changes are allowed when the final reconstruction remains within every limit.

### EVAL_RESULT

Explain that the complete evaluator must finish successfully: probe-enabled
build and gates, probe-free production rebuild, and benchmark. A build failure,
test failure, crash, timeout, missing metric, or incomplete evaluator fails this
gate.

## Hint layering

The factual goal and gate descriptions stay synchronized across all three
files, while search guidance preserves the intended experiment variants:

- `task.yaml` contains a short set of example directions and explicitly permits
  new code-grounded directions.
- `task_hints.yaml` contains richer structural suggestions and FCN rounding-risk
  guidance, while still permitting new directions.
- `task_nohints.yaml` gives no enumerated optimization recipe. It still tells
  the proposer to inspect the production code and find new valid directions.

No configuration should compress the prose enough to hide why a gate exists,
what it measures, or what classes of changes can invalidate it.

## Validation

After editing:

1. Run `simpleloop validate` on all three task files.
2. Scan the active directory for forbidden old-version and deleted-file terms.
3. Confirm the three configurations use identical gate keys, thresholds, source
   path, baseline reference, and evaluator command.
4. Review the diff to ensure no loop or execution settings changed.
