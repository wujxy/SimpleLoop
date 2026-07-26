# OMILREC v1.0.0 Gate Descriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give all three active OMILREC v1.0.0 task configurations complete, consistent goal and gate semantics without obsolete version or implementation language.

**Architecture:** Keep execution settings and metric keys unchanged. Synchronize the factual gate contract across the three YAML files, while preserving three levels of search guidance: moderate, rich, and direction-free.

**Tech Stack:** YAML task configuration, SimpleLoop CLI validation, Python/PyYAML consistency checks, ripgrep terminology checks.

## Global Constraints

- OMILREC v1.0.0 is the only algorithm and numerical truth source.
- Preserve `scripts/sl_eval_v100.sh --evtmax 10`, `simpleloop-v100-gated-baseline`, all loop settings, editable paths, and frozen paths.
- All configurations explain `CONTRACT`, `FCN`, `CONSISTENCY`, and `EVAL_RESULT`.
- Do not mention obsolete version labels, replay packs, deleted split-likelihood files, or a second truth source.
- Hints are examples rather than an exhaustive search space.

---

### Task 1: Synchronize goal and gate prose

**Files:**
- Modify: `examples/omilrec-v100-opt/task.yaml`
- Modify: `examples/omilrec-v100-opt/task_hints.yaml`
- Modify: `examples/omilrec-v100-opt/task_nohints.yaml`

**Interfaces:**
- Consumes: SimpleLoop task schema and `eval.metrics.gates[].description`.
- Produces: three valid configs with identical factual gate requirements and intentionally different hint depth.

- [ ] **Step 1: Run the pre-change acceptance check**

Run a Python/PyYAML check that requires every description to include its
specific semantics: CONTRACT anti-bypass protection; FCN production member,
16 finite unique results, and `1e-13`; CONSISTENCY 18 finite events and all four
tolerances; EVAL_RESULT probe-enabled tests, probe-free build, benchmark, and
failure modes.

Expected: FAIL because the current descriptions are abbreviated and the hints
goal contains obsolete terminology.

- [ ] **Step 2: Rewrite the three goals**

Use a shared factual core: minimize probe-free `SPEED_MS`; v1.0.0 is the sole
truth; all four gates must pass; the evaluator and truth assets cannot be
weakened or regenerated.

Keep hint layering:

- `task.yaml`: mention invariant hoisting, caching, layout, allocation, and
  dead-work removal as non-exhaustive examples.
- `task_hints.yaml`: additionally explain exact-rounding risks such as reduction
  reordering, type/cast changes, nominally equivalent math replacements, and
  constant folding.
- `task_nohints.yaml`: ask for code-grounded new directions without enumerating
  optimization recipes.

- [ ] **Step 3: Expand all four gate descriptions**

Use synchronized substantive requirements:

- `CONTRACT`: production-member linkage, frozen assets/thresholds, complete
  evaluator; modification, reduction, bypass, replacement, or a second
  likelihood test implementation fails.
- `FCN`: real `OMILRECV2::Calculate_EVLikelihood`, four events by four stages,
  exactly 16 finite unique values, relative error `<1e-13`; rounding-changing
  transformations may fail.
- `CONSISTENCY`: all 18 results finite; Euclidean 3D position `<=4 mm`, energy
  `<=7 keV`, t0 `<=10 ps`, peSum `<=0.1 PE`; implementation may change only
  while outputs remain within every limit.
- `EVAL_RESULT`: probe-enabled build/tests, probe-free production rebuild, and
  benchmark all complete; build/test failure, crash, timeout, missing metrics,
  or incomplete execution fails.

- [ ] **Step 4: Run the post-change validation**

Run:

```bash
simpleloop validate --config examples/omilrec-v100-opt/task.yaml
simpleloop validate --config examples/omilrec-v100-opt/task_hints.yaml
simpleloop validate --config examples/omilrec-v100-opt/task_nohints.yaml
```

Expected: all three print `Valid config`.

Run terminology and consistency checks. Expected: no obsolete terms, all gate
keys and fixed execution settings agree, and all required semantics are found.

- [ ] **Step 5: Review and commit only the three configurations**

Run:

```bash
git diff --check -- examples/omilrec-v100-opt
git diff -- examples/omilrec-v100-opt
```

Confirm no loop, source, evaluator command, safety path, or metric key changed.
Commit only the three YAML files with:

```bash
git commit -m "docs: clarify v1.0.0 optimization gates"
```
