# OMILRECV2 post-v1.0.7-gated open optimization task

This task starts from v1.0.0 source and minimizes `SPEED_MS` against the
post-v1.0.7 evaluator package. The Researcher may redesign `OMILRECV2/src/` and
its local CMake wiring; the evaluator, references, thresholds, and root build
wiring are frozen.

The Harness admits a candidate only when `FCN`, `CONSISTENCY`, and
`EVAL_RESULT` all pass, then selects by the measured objective. The gate
descriptions state observable tolerances and deliberately do not prescribe a
research method.

```bash
simpleloop validate --config examples/omilrec-post-v107-opt/task.yaml
simpleloop run --config examples/omilrec-post-v107-opt/task.yaml \
  --run-dir ./runs/omilrec-postv107-001
```

Ten-event timings guide the search. Confirm a final candidate with longer
repeated measurements on the same host.
