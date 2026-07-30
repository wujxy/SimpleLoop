# Current-Version Test Audit Design

## Goal

Keep only tests that protect SimpleLoop's latest-version behavior. Remove
tests whose only purpose is backward compatibility, personal-site
configuration, obsolete implementation wiring, or coverage already supplied
by an equivalent test.

## Effective-test criteria

A test remains when it protects at least one current observable contract:

- configuration validation or CLI behavior;
- safety boundaries, atomic persistence, locking, or secret isolation;
- candidate selection, gates, concurrent execution, resume, or HEPJob state;
- current history, telemetry, plotting, prompt, or role-boundary behavior;
- a distinct failure mode that another retained test does not exercise.

Fine-grained tests are not removed merely because they are specific. State
machine transitions and security/error boundaries need focused tests.

A test is removed when it only:

- accepts or renders data written by an older SimpleLoop version;
- asserts one user's absolute paths or site-specific README text;
- patches an implementation entry point that the current code no longer uses;
- invokes real checked-in site configuration instead of isolating the unit;
- duplicates a current contract already covered by a retained test.

## Deletions

Delete `tests/test_accidental_overwrite_protection.py` in full. Its three tests
invoke a real OMILREC configuration and missing SIF instead of isolating the
overwrite decision. Two tests also catch broad `ValueError` results and return
`True`, so they can pass without demonstrating a successful run.

Delete these backward-compatibility tests:

- `tests/test_agent_usage.py::test_decode_output_keeps_legacy_plain_json_and_missing_usage`
- `tests/test_telemetry.py::test_resume_from_legacy_null_total_starts_known_category_counts`
- `tests/test_views_and_parse.py::test_store_changed_paths_missing_key_defaults_empty`
- `tests/test_views_and_parse.py::test_parse_accepts_legacy_feedback_without_prefix`
- `tests/test_plot.py::test_build_series_without_objective_schema_still_tracks_scores`
- `tests/test_plot.py::test_worktime_rebase_offsets_lifts_resume_drop`
- `tests/test_plot.py::test_worktime_stays_continuous_across_continue_resume`
- `tests/test_plot.py::test_history_without_telemetry_keeps_round_data`

Delete these site-specific tests:

- `tests/test_example_apptainer.py::test_omilrec_configs_bind_large_external_roots`
- `tests/test_example_apptainer.py::test_omilrec_readmes_name_required_external_binds`

Delete the obsolete implementation-wiring test explicitly identified by the
user:

- `tests/test_static_mode.py::test_static_mode_accepts_by_gates_alone_and_records_generations`

Delete these duplicate tests:

- `tests/test_views_and_parse.py::test_parse_batch_enforces_exact_count`,
  already covered by the accepted-shape and parameterized count tests in
  `tests/test_parallel_candidates.py`;
- `tests/test_views_and_parse.py::test_config_gate_description_is_parsed_and_optional`,
  already covered by the post-v107 example contract test in
  `tests/test_parallel_candidates.py`.

## Retained boundaries

Retain current strict-rejection tests even when their input resembles an old
format. Rejecting an invalid current input is a current contract, not backward
compatibility. In particular, retain proposer tests that reject legacy
single-proposal shapes and judger tests that reject missing required fields.

Retain all distinct HEPJob lifecycle transitions, container environment
filtering, runtime preflight, atomic output, locking, selection, gate,
concurrency, current continue-mode, telemetry persistence, and plotting output
tests.

## Scope and verification

No production files, configuration, secrets, or deployment settings change.
After deletion:

1. collect the suite to confirm every intended test disappeared and no
   unrelated test was lost;
2. run `python -m pytest -q tests/`;
3. inspect the diff to ensure it contains test deletions and audit
   documentation only.

Only after the remaining suite passes may `v0.1.1` be moved to the latest
`ihep_scale`, merged into `rsi-prompt`, and the resulting Git graph verified.
