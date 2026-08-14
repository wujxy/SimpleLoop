# SimpleLoop Phase 7 convergence implementation plan

**Goal:** Activate `simpleloop.v1`, isolate legacy config parsing, remove the
last transition packages, and prove the complete pipeline still runs.

**Architecture:** Keep one public config boundary and one normalized runtime
shape. Move existing code to its final owner, merging only modules whose
responsibilities are already identical. Do not retain forwarding packages.

## 1. Lock the Phase 7 boundary with failing tests

- Add new-schema resolution tests for local, HPC, RSI, strict unknown fields,
  and legacy delegation.
- Extend architecture tests to require final package removal and forbid old
  imports.
- Run the focused tests and confirm the new expectations fail.

## 2. Implement the configuration boundary

- Keep the old entry adapter in `simpleloop/legacy_config.py` and shared
  normalized validation in a private module removable independently of it.
- Implement strict `simpleloop.v1` translation in `simpleloop/config.py`.
- Keep resolved-snapshot reading in `config.py` and legacy snapshot lifting.
- Wire the translated RSI block to the existing Phase 6 pipeline.
- Run config, initialization, provenance, and architecture tests.

## 3. Converge package ownership

- Merge history read/query/store in `persistence/history.py`.
- Merge raw evaluation execution into `stages/evaluator.py`.
- Move agent, gate view, handoff, export, and image code to their final owners.
- Update production and test imports.
- Remove `harness`, `roles`, and `container` completely.
- Run focused tests after each ownership group.

## 4. Migrate examples and public documentation

- Convert active example task YAML files to `simpleloop.v1`.
- Update README/schema guidance and remove current references to old packages.
- Keep historical design documents unchanged except where they claim current
  paths.

## 5. Verify end to end

- Run `python -m compileall simpleloop proposer`.
- Run the complete pytest suite.
- Run the repository's self-contained tiny-loop smoke path.
- Inspect `git diff --check`, imports, deleted directories, and worktree status.
- Commit Phase 7 as the final refactor phase.
