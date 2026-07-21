# OMILREC v1.0.0 Post-v1.0.7 Gate Package Design

## Goal

Create an independent OMILRECV2 optimization package that starts from the
unoptimized v1.0.0 algorithm source, while using post-v1.0.7-style FCN and
end-to-end reconstruction gates for SimpleLoop optimization runs.

## Package Boundary

The new package lives at:

`/datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated`

It must not reuse or mutate the existing `omilrec-v100` working tree. The
package is created from committed v1.0.0 source state only, then receives its
own gate harness and wrapper changes.

## Starting Point

The algorithm source starts from `omilrec-v100` tag `v1.0.0` /
`v1.0.0-base`, commit `b51f3b8`. This preserves the intended optimization
meaning: SimpleLoop candidates optimize v1.0.0-era algorithm code, not the
already optimized post-v1.0.7 package.

## Gate Contract

The new package provides one eval wrapper:

`scripts/sl_eval_post_v107.sh`

The wrapper emits machine-parseable lines:

- `FCN=PASS|FAIL|NA`
- `CONSISTENCY=PASS|FAIL|NA`
- `SPEED_MS=<float>|NA`
- `EVAL_RESULT=ok|fcn_fail|consistency_fail|build_fail|bench_fail|...`

The intended hard gates for SimpleLoop are `FCN`, `CONSISTENCY`, and
`EVAL_RESULT`.

The FCN gate uses the post-v1.0.7 frozen fixture model. The reconstruction gate
uses the post-v1.0.7 relaxed tolerances:

- energy <= 0.007 MeV
- x/y/z <= 4.0 mm
- t0 <= 0.01 ns
- peSum <= 0.1 PE

## SimpleLoop Integration

Add a new SimpleLoop config:

`SimpleLoop/examples/omilrec-v100-postv107-gated.yaml`

The config must point only to the new package:

`source.path: ../../omilrec-v100-postv107-gated`

The eval command must be package-relative:

`bash scripts/sl_eval_post_v107.sh --evtmax 10`

No eval command should contain an absolute path to `omilrec` or `omilrec-v100`.

## Manual Chain Validation

After package creation, run a real local chain:

1. Make a small admissible source-code change in the new package.
2. Run the new package eval wrapper.
3. Revert that small source-code change.
4. Confirm the wrapper exercised build, FCN/reconstruction gates, and speed
   parsing from the new package, not from `omilrec` or `omilrec-v100`.

The manual change is only a chain check and must not remain in the final package
unless it is intentionally harmless documentation or formatting.
