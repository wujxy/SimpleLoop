# Proposer Protocol Repair Design

Date: 2026-08-01

## Goal

Prevent one malformed Proposer response from aborting an otherwise healthy
run, while preserving the rule that SimpleLoop executes only one explicit,
schema-valid action at a time.

This change addresses the failure observed in `test005.log`: HEPAI returned a
valid JSON value followed by extra content, and the strict parser aborted the
second round.

## Scope

The change has two necessary layers:

1. Ask HEPAI for a single JSON object with
   `response_format={"type": "json_object"}`.
2. If local action validation still fails, let the Proposer correct its
   response at most twice within the same agent step.

The existing local action parser remains authoritative. Structured output is a
transport constraint, not a replacement for Runtime validation.

## Runtime Semantics

For each Proposer step:

1. Call the model for one action.
2. Validate the complete response as exactly one JSON object and then validate
   its action-specific fields.
3. On success, execute the action normally.
4. On protocol failure, execute nothing. Append the rejected assistant response
   and a compact correction request to the in-memory conversation, then call
   the model again.
5. Allow at most two correction calls for the same step. If both fail, raise a
   `ProposerError` and fail closed.

Only a valid action advances `max_steps`. Correction calls still consume the
existing total Proposer deadline and their provider-reported token usage is
recorded normally.

The correction request identifies the validation reason and repeats the
requirement to return exactly one action object. It does not prescribe which
scientific action the Proposer should choose.

## Safety and Observability

The Runtime must not:

- extract the first object and ignore trailing content;
- strip arbitrary prose or Markdown fences;
- accept concatenated JSON objects;
- infer or execute a likely action;
- retry transport errors as protocol errors;
- print or persist the rejected model response.

Each correction prints only metadata, for example:

```text
[proposer step 1/20] protocol repair 1/2 reason=invalid_json
```

The reason is a fixed internal category, not provider- or model-controlled
text. Existing safe action summaries remain unchanged.

## HEPAI Compatibility

The installed HEPAI client exposes the OpenAI-compatible `response_format`
parameter. Before relying on it, implementation verification must make one
minimal live request against the configured HEPAI endpoint. If the endpoint or
configured model rejects JSON object mode, that is a compatibility failure to
report; the implementation must not silently switch to a second transport
protocol.

## Error Boundary

Protocol correction applies to local response-contract failures, including:

- invalid JSON or extra data;
- a non-object JSON value;
- missing, unknown, or invalid `action`;
- invalid action keys or argument values;
- the wrong number of submitted proposals.

Network failures, authentication failures, provider timeouts, and empty
responses remain model transport failures and are not retried by this
mechanism.

After two unsuccessful corrections, the run aborts rather than retrying
forever or executing ambiguous output. This is the strongest safe guarantee:
one malformed response is recoverable, while repeated non-compliance remains a
visible failure.

## Minimal Code Changes

- `simpleloop/roles/model.py`: add JSON object mode to the HEPAI completion
  request.
- `simpleloop/roles/proposer.py`: add the bounded same-step correction loop and
  fixed-category safe status output.
- `tests/test_model.py`: assert that HEPAI requests JSON object mode.
- `tests/test_proposer_agent.py`: cover successful correction, two failed
  corrections, step accounting, usage accounting, no action execution before a
  valid response, and safe output.

No configuration field, provider registry, generic retry framework, batch
action schema, persisted trace, or compatibility parser is added.

## Acceptance Criteria

- The `test005.log` response shape (one JSON object plus extra data) triggers a
  correction instead of immediately aborting.
- A corrected response executes exactly once and consumes one agent step.
- Two failed corrections abort safely without executing an action.
- HEPAI requests JSON object mode.
- Transport failures retain their current behavior.
- Raw rejected content never appears in terminal output or persisted history.
- Existing Proposer and project tests continue to pass.
