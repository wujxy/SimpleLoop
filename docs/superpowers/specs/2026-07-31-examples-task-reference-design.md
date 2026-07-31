# Complete `examples/task.yaml` Reference

## Goal

Maintain `examples/task.yaml` as the single complete reference for every
configuration field currently accepted by `simpleloop/config.py`.

## Structure

The template follows the runtime flow:

1. `kind` and `task`
2. `safety`
3. `loop`
4. `runtime`
5. `eval`
6. `source`
7. `execution`
8. `prompt_self_improvement`

Required local-run fields remain active. Optional fields are shown with their
defaults, constraints, and purpose. HEPJob fields remain commented under an
active `execution.backend: local` so copying the reference does not
accidentally submit remote jobs.

The prompt self-improvement block remains an enabled example and documents all
six supported fields, trigger semantics, relative paths, and the requirement
that prompt and history directories do not overlap.

## Boundaries

- Document only fields accepted by the strict config parser.
- Keep explanations adjacent to their fields.
- Do not add new configuration behavior.
- Preserve concrete workload examples in their existing subdirectories.

## Verification

Add a test that parses the YAML and checks that the reference contains every
currently supported field, including all HEPJob and prompt self-improvement
options. Run the config tests and parse the completed template with temporary
valid source/runtime paths.
