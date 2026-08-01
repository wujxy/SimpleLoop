# Lightweight Proposer Agent Runtime

Date: 2026-08-01
Status: pending written-spec review

## Objective

Turn the current one-shot Proposer into a lightweight, stateful scientific
agent without changing SimpleLoop's outer experimental topology:

```text
outer loop
  Proposer Agent Runtime
      -> proposal batch
  Worker / Executor
      -> candidate SHA
  Harness evaluation and Gates
      -> factual experiment history
  next Proposer round
```

The Proposer may investigate adaptively, use a sandboxed shell, maintain
compact Insights, and decide when it has enough evidence to submit an
experiment. It cannot call the Worker, modify the accepted artifact, run the
authoritative Harness path, select a parent, or declare a Gate result.

This version is a foundation for a future fully agentic optimizer. It does not
move outer-loop scheduling into the Proposer.

## First-principles boundary

SimpleLoop separates three kinds of authority:

```text
Research freedom       Proposer chooses what and how to investigate
Implementation         Executor changes the candidate artifact
Factual authority      Harness alone evaluates, gates, and selects
```

The scientific process is not a fixed state machine. Reading source, comparing
SHAs, searching history, running a scratch analysis, revising a hypothesis, and
writing an Insight are available actions, not mandatory ordered steps.

The stable scientist system prompt internalizes only epistemic principles:

- distinguish fact, observation, hypothesis, inference, and uncertainty;
- investigate evidence instead of trusting prior narrative;
- use failed experiments as evidence;
- change or abandon a research direction when evidence warrants it;
- optimize for useful experiments, not compliance with a reflection template;
- treat only Harness outputs as authoritative experimental facts;
- choose the investigation process adaptively.

The Runtime enforces permissions, budgets, persistence, and the terminal output
contract. The prompt does not enforce those security properties.

## Architecture

```text
HEPAI Chat Model
      ^  one JSON action per completion
      |
Proposer Agent Runtime
      |
      +-- run_research_command  -> isolated Apptainer shell
      +-- search_history        -> compact factual matches
      +-- inspect_episode       -> one persisted factual episode
      +-- write_insight         -> one pending semantic memory record
      +-- submit_proposals      -> terminal action
      |
      +-- recent factual view   <- history.jsonl (Harness-owned)
      +-- accumulated Insights  <- insights.jsonl (Proposer-authored)
```

The existing Claude Code adapter remains for the Executor. The Proposer no
longer uses Claude Code.

## Model transport

### First backend

The first production backend is HEPAI:

- Python client: `hepai`;
- project dependency: `hepai>=1.1.13`;
- base URL: `https://aiapi.ihep.ac.cn/apiv2`;
- default model: `gpt-5.5`;
- API key: host environment variable `HEPAI_API_KEY`;
- API: non-streaming `client.chat.completions.create(...)`.

The key is never accepted as a task-config value, written to a resolved config,
printed, forwarded into the research shell, or submitted to HEPJob workers.
The model call runs in the frontend Python process; research commands run in a
separate sandbox.

### Why non-streaming JSON actions

The verified HEPAI contract includes system/user messages and streamed or
non-streamed Chat Completions, but its public user guide does not specify a
function-calling contract. The MVP therefore does not implement both native
tool calls and a fallback parser.

Every model completion must contain exactly one JSON object describing one
Runtime-owned action. Examples:

```json
{"action":"run_research_command","command":"git grep -n hot_loop","cwd":"source"}
```

```json
{"action":"inspect_episode","ref":"r12c1"}
```

```json
{"action":"search_history","query":"sparse gather"}
```

```json
{"action":"write_insight","text":"Packing helped only when it removed a sparse gather.","refs":["r8c0","r12c1"]}
```

```json
{"action":"submit_proposals","proposals":["Replace the sparse gather with ..."]}
```

Malformed JSON, an unknown action, schema-invalid arguments, transport failure,
or a non-action response fails the Proposer call. A schema-valid action whose
operation fails returns a bounded observation. The first version does not add
repair prompts, retries, or a second protocol; evidence from real runs may
justify those later.

### Minimal future-provider boundary

One narrow internal interface is necessary because the user requires later
support for multiple model interfaces:

```python
class ChatModel(Protocol):
    def complete(self, *, system: str, messages: list[dict]) -> ModelReply: ...
```

Only `HepAIChatModel` is implemented now. There is no provider registry,
automatic routing, fallback model, capability negotiation, or provider-specific
configuration hierarchy. Future clients adapt their response into `ModelReply`
without changing the scientific Runtime or action protocol.

`ModelReply` contains only assistant text and provider-reported usage. Usage is
fed into the existing telemetry observer; provider-specific usage semantics are
not interpreted by the agent loop.

## Agent loop

For each outer round, the Runtime:

1. builds a system prompt from the stable scientist constitution;
2. builds the initial user context from Goal, Gates, editable/frozen paths,
   parent SHA, accumulated Insights, and recent factual outcomes;
3. calls the model for one JSON action;
4. validates and executes the action;
5. appends the assistant action and a bounded tool observation to the message
   transcript;
6. repeats until `submit_proposals` or a hard budget is exhausted.

The Runtime does not prescribe which non-terminal action comes first or require
that every action type be used.

`submit_proposals` must contain exactly `candidates_per_round` nonblank strings.
It is the only successful terminal action. Step exhaustion, total timeout, or
model/tool failure aborts the round before a candidate generation is recorded.

The existing `loop.agent_timeout_seconds` is the total Proposer deadline. A new
`researcher.max_steps` bounds model spend and prevents an unending internal
loop. Each shell call is additionally bounded by
`researcher.command_timeout_seconds`; its effective deadline cannot exceed the
remaining Proposer deadline.

## Tool set and necessity

### `run_research_command`

Runs `bash -lc <command>` in an isolated research container. It is necessary
because a finite set of predefined source tools would constrain an open
Researcher to investigation methods anticipated by SimpleLoop's authors. It
supports Git queries, project tools, temporary parsers, compilation probes,
binary inspection, and exploratory measurements.

The action selects one working directory:

- `source`: current accepted source snapshot, read-only;
- `scratch`: ephemeral per-Proposer-call working directory, writable.

Output contains exit code plus bounded stdout/stderr. Truncation is explicit.
The command is an informal observation, never a Harness fact.

### `search_history`

Searches a deterministic compact projection of every persisted candidate and
returns bounded matches with `r<round>c<candidate>` references. It is necessary
because Insights may be absent, incomplete, or wrong; old factual episodes
must remain discoverable without injecting all history into every prompt.

`query` is a required nonblank string. Matching is case-insensitive substring
search over episode ref, proposal, status, changed paths, metric keys, and Gate
keys. Results are ordered newest first and capped at 20. Semantic search,
ranking, pagination, and configurable query syntax are not added.

### `inspect_episode`

Resolves one `r<round>c<candidate>` reference and returns the complete persisted
candidate episode available in `history.jsonl`, including its bounded eval
text. It is necessary to recover detailed evidence on demand while keeping the
default context bounded. Source truth is inspected separately through the
episode SHA and the research shell.

### `write_insight`

Creates or replaces the single pending Insight for the current Proposer call.
It is necessary for semantic continuity across a long run and supplies keys
back to supporting factual episodes. It is optional: no new reusable
understanding means no Insight.

The pending record is limited to 500 characters and requires at least one valid
episode reference. This keeps every persisted Insight useful as a historical
index and prevents an ungrounded hypothesis from being presented as learned
memory. Multiple calls revise the same pending record instead of creating
multiple memories. On successful proposal submission it becomes the
append-only record `I<round>`; old Insights are never edited. A later Insight
may correct an earlier one.

### `submit_proposals`

Returns the exact proposal batch to the existing outer loop. It is necessary
as the explicit boundary between autonomous investigation and candidate
execution. It does not call the Worker itself.

No separate `read_file`, `search_code`, `git_diff`, script runner, notebook,
planner, or reflection tools are added. The shell already covers source-side
investigation; duplicating those actions would expand code without expanding
capability.

## Research shell isolation

Raw host Bash is not exposed. A dedicated Apptainer command builder creates:

```text
/source    current parent snapshot, read-only
/repo      per-run Git object store, read-only
/history   run facts and artifacts, read-only
/scratch   ephemeral per-Proposer-call directory, writable
```

Configured task binds are mounted read-only for research. The container uses a
contained home and temporary filesystem, a sanitized environment, no HEPAI or
Anthropic credentials, no scheduler credentials, and no Worker control handle.
Network is disabled for research commands. Background processes are terminated
with the command process group when the command ends or times out.

The accepted source snapshot is materialized by outer-loop code before entering
the sandbox and removed afterwards. Attempts to mutate source, repo, history,
or run state fail through filesystem permissions rather than prompt advice or
command-string blacklists.

Scratch is deleted after the Proposer call whether it succeeds or fails. The
MVP persists only Insights, proposals, normal telemetry, and Harness facts; it
does not create a second unmanaged research-artifact store.

## Memory and context

### Factual episodic memory

`history.jsonl` remains Harness-owned and append-only. The model receives only
the existing recent factual projection by default, bounded by
`loop.proposer_recent_rounds`.

Historical details are retrieved through `search_history`, `inspect_episode`,
and Git SHA inspection. Proposer-authored text cannot change eligibility,
selection, metrics, or Gates.

### Semantic memory

`insights.jsonl` is Proposer-authored and append-only. Each record is:

```json
{"id":"I13","text":"...","refs":["r11c0","r12c2"]}
```

All Insights are injected into the initial Proposer context. At most one
500-character record may be added per outer round, so the configured finite
`max_rounds` bounds context growth. The first version does not add Insight
compaction, ranking, embeddings, TTLs, mutation, or a Memory Agent.

The initial context therefore has three memory levels:

```text
all compact Insights       long-term semantic index
recent factual rounds      short-term detailed evidence
on-demand episode lookup   complete older persisted facts
```

An Insight is explicitly labeled as a fallible interpretation. Its references
are navigation keys, not proof that its text is true.

## Outer-loop integration and recovery

`ProposerAgent.run(...)` replaces the current one-shot `propose(...)` call and
returns:

```python
ProposerResult(proposals: list[str], insight: Insight | None, usage: object)
```

The outer loop remains responsible for candidate scheduling. Proposal strings
flow unchanged into the existing local or HEPJob backend.

The result's pending Insight is stored with proposals in the existing in-flight
round metadata. It is appended to `insights.jsonl` exactly once when the round
reaches a business-terminal history record. Resuming an in-flight round skips
the Proposer and preserves the same proposals and pending Insight. A crash
before in-flight state exists simply reruns the Proposer and leaves no partial
Insight.

Failed Gates, failed execution, and non-improving candidates still enter
factual history. Their results become available to the next Proposer round.

Static-proposal mode does not invoke the Proposer and writes no Insight.

## Configuration

An agent-driven task adds one strict top-level block:

```yaml
researcher:
  api: hepai
  model: gpt-5.5
  base_url: https://aiapi.ihep.ac.cn/apiv2
  max_steps: 20
  command_timeout_seconds: 120
  command_output_cap_chars: 12000
```

Necessity:

- `api` makes the single implemented transport explicit and reserves a clean
  validation boundary for a future transport without adding a registry;
- `model` and `base_url` are required to address the user-selected service;
- `max_steps` prevents an unbounded model loop and unbounded API spend;
- command timeout prevents runaway probes;
- command output cap prevents one shell observation from exhausting context.

`HEPAI_API_KEY` is required in the frontend environment and validated before
the baseline evaluation of a normal agent-driven run. Static-proposal mode does
not construct the Proposer Runtime and therefore requires neither the
`researcher` block nor HEPAI credentials. When the block is present, config
validation remains strict; a normal run without it fails before baseline work.

Existing `loop.agent_timeout_seconds`, `loop.proposer_recent_rounds`, and
`loop.candidates_per_round` retain their current meanings. No temperature,
retry count, Insight count, prompt strategy, provider fallback, or separate
token-budget configuration is introduced.

## Code boundaries

The implementation should keep four focused responsibilities:

- `simpleloop/roles/proposer.py`: scientific context, JSON action schemas, and
  the internal agent loop;
- `simpleloop/roles/model.py`: minimal `ChatModel` protocol and HEPAI transport;
- `simpleloop/roles/research_tools.py`: tool dispatch, factual lookup, Insight
  buffering, and sandboxed command execution;
- `simpleloop/harness/memory.py`: Harness-owned history reading and factual
  episode projection only.

Insight persistence must not be placed under `harness`: the Harness may store
the Proposer's opaque pending record transactionally, but it does not author or
interpret semantic memory.

The existing `roles/agent.py` remains the Claude Code Executor adapter. It is
not generalized into a universal agent abstraction.

The editable `prompts/proposer.md` remains the semantic scientist constitution
used as the system prompt. Runtime action syntax, tool authority, and Harness
authority are assembled by immutable Python code, so prompt self-improvement
cannot redefine the protocol or grant new tools.

## Failure behavior

- Missing/invalid HEPAI configuration: fail during config/preflight.
- HEPAI transport error: fail the Proposer call; no candidate round consumed.
- Malformed or unknown JSON action: fail the Proposer call.
- Invalid tool arguments: return a bounded tool error observation so the agent
  may choose another action; filesystem and security violations remain denied.
- Shell nonzero exit: return it as an observation, not a Runtime failure.
- Shell timeout: kill the process group and return a timeout observation.
- Step or total deadline exhausted: fail the Proposer call.
- Invalid proposal count/content: fail the terminal action.
- Invalid Insight refs: reject that tool action; do not persist it.
- Insight append replay with identical `I<round>`: no-op; conflicting content
  for the same ID is an error.

There is no automatic model retry, protocol fallback, or silent coercion.

## Verification strategy

Tests use a fake `ChatModel`; unit tests never call HEPAI or require a key.

Required coverage:

1. HEPAI client receives system/user messages, configured model/base URL, and
   reads the key only from `HEPAI_API_KEY`.
2. HEPAI key never appears in config snapshots, Runtime summaries, tool
   environments, telemetry, errors, or HEPJob environment files.
3. The agent accepts arbitrary valid tool order and does not require a
   reflection/Insight sequence.
4. JSON action validation rejects prose, unknown actions, and extra fields.
5. Research shell can read source/history and write scratch, but cannot mutate
   source, repo, history, or run state.
6. Command timeout, process-group termination, and output caps are enforced.
7. Search finds old candidates outside the recent prompt window.
8. Episode lookup returns one persisted candidate without injecting unrelated
   history.
9. `write_insight` is optional, revisable within a call, bounded, requires a
   valid historical reference, and is append-only across rounds.
10. Initial context contains all Insights plus only configured recent facts.
11. `submit_proposals` returns exact K strings and never invokes a Worker.
12. In-flight resume preserves proposals and pending Insight without rerunning
    the Proposer or duplicating `I<round>`.
13. Static proposals bypass the Runtime and create no Insight.
14. Harness Gates and deterministic parent selection are unchanged.
15. Existing local/HEPJob, reporting, export, prompt self-improvement, and
    Executor tests continue to pass.

## MVP necessity audit

| Added element | Why it is necessary | What is deliberately not added |
|---|---|---|
| Internal agent loop | Lets the Researcher investigate before proposing | self-recursion across outer rounds |
| Stable scientist system prompt | Internalizes epistemic identity | mandatory reflection chain |
| HEPAI transport | Provides the selected raw model interface | multi-provider registry/fallback |
| Runtime-owned JSON actions | Works on the verified text contract and keeps tools provider-independent | native-tools plus fallback dual path |
| Sandboxed Bash | Preserves open-ended research actions | raw host Bash or command blacklist |
| Search + episode lookup | Makes old facts discoverable without full-history injection | vector DB, embeddings, knowledge graph |
| One optional Insight | Provides compact semantic continuity and history keys | multiple memory types, Memory Agent, compaction |
| Step/time/output limits | Bound spend, processes, and context | adaptive budget controller |
| Read-only source/history + scratch | Enforces role authority while allowing experiments | persistent research filesystem |
| Minimal `ChatModel` protocol | Meets the explicit future multi-interface requirement | provider plugins, routing, negotiation |

Removing any item in the first column would either collapse the feature back
to a one-shot proposal generator, lose long-run research memory, make old facts
undiscoverable, or violate the authority/resource boundary. Everything in the
third column waits for observed need.

## Non-goals

- Proposer calling Worker or Harness;
- removing the outer loop;
- persistent live model sessions across rounds;
- planner, critic, reviewer, Judger, or Memory Agent roles;
- Harness or Gate evolution;
- modifying code from the Proposer sandbox;
- network or literature-search tools;
- persistent scratch artifacts;
- multiple model providers in the first implementation;
- native HEPAI/OpenAI function calling;
- retry/fallback/repair policy;
- old semantic-Insight schema compatibility;
- automatic Insight truth scoring, merging, retirement, or deletion.

## Acceptance invariants

- Runtime topology remains Proposer Agent -> Worker -> Harness.
- The Proposer owns investigation and interpretation but not factual authority.
- No fixed scientific action order is encoded in Python or the prompt.
- The Proposer can perform unanticipated investigations through sandboxed Bash.
- Filesystem permissions, not prompt instructions, protect source and history.
- Harness facts and Proposer Insights remain separate and visibly labeled.
- Default context is recent factual history plus all bounded Insights; older
  details are retrieved on demand.
- Only `submit_proposals` crosses from research into outer-loop execution.
- Only the HEPAI transport is implemented, behind the smallest interface needed
  for a later provider.
- Every added capability has a stated MVP necessity and no speculative
  subsystem is introduced.
