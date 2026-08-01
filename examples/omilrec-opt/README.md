# OMILRECV2 v1.0.0 open optimization task

This task starts from the unoptimized v1.0.0 reconstruction and minimizes
`SPEED_MS`. The Researcher may redesign any file under `OMILRECV2/src/` and its
local CMake wiring. Tests, evaluator scripts, references, thresholds, and root
build wiring remain frozen.

The Harness requires both `CORRECTNESS` and `EVAL_RESULT` to pass. It records
every attempt and selects the lowest eligible `SPEED_MS`; rejected attempts do
not advance the parent chain.

```bash
export HEPAI_API_KEY='<your-key>'
simpleloop validate --config examples/omilrec-opt/task.yaml
simpleloop run --config examples/omilrec-opt/task.yaml \
  --run-dir ./runs/omilrec-opt-001
```

For a controlled replay instead of live Researcher proposals:

`HEPAI_API_KEY` is not required for this static mode.

```bash
simpleloop run --config examples/omilrec-opt/task.yaml \
  --proposals examples/omilrec-opt/omilrec-paper-proposals-main.yaml \
  --run-dir ./runs/omilrec-paper-replay
```

The ten-event benchmark is a search signal and can be noisy. Confirm a final
candidate with longer repeated measurements on the same host.
