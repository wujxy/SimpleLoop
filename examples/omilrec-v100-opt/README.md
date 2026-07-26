# OMILREC v1.0.0 optimization task

This is the only task to use for the rebuilt gate. Algorithm truth, FCN golden
values, the E2E reference, and the performance baseline all originate from
OMILREC v1.0.0.

Evaluation runs the static contract, 16 direct production-member FCN checks,
and 18 E2E checks with a test probe build. It then rebuilds without the probe
and reports `SPEED_MS` and `SPEEDUP_V100`. The authoritative v1.0.0 median is
919.94892 ms/event from three single-thread 100-event repetitions. Ten-event
round measurements are only a search signal; confirm a final candidate with
long repeated runs on the same host.

The older OMILREC task directory and its runs are retained untouched but have
an invalid gate and must not be used as evidence or as a new-run configuration.

```bash
simpleloop validate --config examples/omilrec-v100-opt/task.yaml
simpleloop run --config examples/omilrec-v100-opt/task.yaml \
  --run-dir ./runs/omilrec-v100-001
```
