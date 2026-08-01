# Tiny algorithm optimization example

This self-contained task optimizes `tinyalgo.count_pairs` for lower
`ms_per_call`. The Researcher proposes experiments, the Executor edits only the
package source, and the Harness runs correctness, drift, and benchmark commands.
Only candidates passing `CORRECTNESS` and `DRIFT` are eligible for
objective-based selection.

```bash
export HEPAI_API_KEY='<your-key>'
simpleloop init --config examples/tiny_algo_opt/task.yaml
simpleloop run --config examples/tiny_algo_opt/task.yaml \
  --run-dir ./runs/tiny-001
```

Trace the factual experiment history with:

```bash
git -C runs/tiny-001/repo log --oneline
cat runs/tiny-001/history.jsonl
```
