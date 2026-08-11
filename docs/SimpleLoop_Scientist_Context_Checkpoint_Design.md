# SimpleLoop Scientist Context Checkpoint Design

## 1. Motivation

Current Scientist-Proposer vNext keeps the full investigation transcript
in active context. During long research runs, tool outputs, source
snippets, intermediate reasoning, and phase transitions accumulate,
causing context growth and anchoring.

The goal of Context Checkpoint is not to summarize past conversation. It
is to restore the Scientist's current scientific state.

Core principle:

> ScientificState is memory; raw transcript is scratchpad.

------------------------------------------------------------------------

## 2. Design Principles

### 2.1 ScientificState is the memory

The existing structured state:

-   Understanding
-   Working Model
-   Explanation Set
-   Lever Map
-   Hypothesis Portfolio
-   Selection
-   Evidence References

is the Scientist's durable cognitive state.

Future reasoning should depend on this state rather than the complete
transcript.

### 2.2 Evidence is addressable, not resident

Raw tool outputs and source dumps should not permanently occupy context.

    Tool output
        ↓
    Evidence reference
        ↓
    ScientificState
        ↓
    Retrieve when needed

### 2.3 Compact at epistemic boundaries

Do not compact arbitrarily by token count.

Natural checkpoints:

    EXPLORE → NARROW
    NARROW  → DEEPEN

------------------------------------------------------------------------

## 3. Target Architecture

Before:

    Conversation Context

    Goal
    Prompt
    Tool outputs
    Source dumps
    Reasoning
    ScientificState
    History

After:

    Scientist Context

    Identity
    Goal / Gate

    ScientificState
     ├── Understanding
     ├── Working Model
     ├── Explanation Set
     ├── Hypothesis Portfolio
     └── Evidence Index

    Recent interaction window

    Historical evidence references

External workspace stores:

-   source files
-   tool outputs
-   logs
-   experiment records

------------------------------------------------------------------------

## 4. Checkpoint 1: EXPLORE → NARROW

Purpose:

Convert system understanding, models, explanations, and hypothesis
generation into a compact scientific worldview.

Discard:

-   old messages
-   raw shell output
-   source dumps
-   intermediate reasoning

Keep:

-   Understanding
-   Working Model
-   Explanation Set
-   Lever Map
-   Fresh Hypothesis Portfolio
-   Evidence Index

------------------------------------------------------------------------

## 5. Checkpoint 2: NARROW → DEEPEN

Purpose:

Convert:

    many hypotheses

into:

    one selected research direction

Keep:

-   selected hypothesis
-   selection rationale
-   relevant evidence
-   critical premise
-   prediction
-   rejected alternative summary

Discard unrelated exploration history.

------------------------------------------------------------------------

## 6. Scientist Context Builder

Introduce:

    simpleloop/context/
        scientist_context.py

with:

``` python
class ScientistContextBuilder:
    def build_checkpoint_context(self, state, phase):
        ...
```

Responsibilities:

-   render ScientificState
-   rebuild Scientist context
-   inject evidence references
-   preserve short-term continuity

No LLM summarization.

------------------------------------------------------------------------

## 7. Evidence Registry

Tool results become addressable artifacts.

Example:

``` json
{
  "evidence_id": "E037",
  "type": "workspace",
  "source": "OMILRECV2/src/foo.cc",
  "location": "line 120-200",
  "created_phase": "MODEL",
  "referenced_by": ["M2", "H5"]
}
```

Large outputs can be retrieved again when required.

------------------------------------------------------------------------

## 8. Recent Context Window

Keep only recent interaction history:

    last 3-5 turns

Purpose:

-   maintain current action continuity
-   avoid complete cold restart

Do not preserve full transcript.

------------------------------------------------------------------------

## 9. Emergency Compact

Add a safety mechanism:

    if context_tokens > threshold:
        trigger emergency checkpoint

Emergency compact:

-   preserves current phase
-   rebuilds context
-   does not change Scientist state

------------------------------------------------------------------------

## 10. Explicitly Not Included

This MVP does not introduce:

-   vector memory
-   LLM-generated summaries
-   additional research agents
-   automatic importance scoring

Reason:

The current ScientificState already provides a structured
representation.

------------------------------------------------------------------------

## 11. Evaluation

Compare before and after.

Metrics:

### Context

-   maximum context tokens
-   total token consumption

### Scientific behavior

Evaluate:

-   Working Model preservation
-   hypothesis quality
-   evidence usage
-   ability to revise beliefs

### Research outcome

Compare:

-   proposal quality
-   abstraction level
-   cost
-   optimization result

------------------------------------------------------------------------

## 12. Implementation Plan

1.  Create `ScientistContextBuilder`.
2.  Implement EXPLORE → NARROW checkpoint.
3.  Implement NARROW → DEEPEN checkpoint.
4.  Replace long transcript dependency with ScientificState + Evidence
    Index.
5.  Run OMILREC Scientist-Proposer benchmark.

------------------------------------------------------------------------

## Expected Outcome

The goal is not to make Scientist remember more.

The goal is:

> Keep Scientist in its best current scientific state during long
> investigations.

Final architecture:

    Raw investigation
            |
            v
    ScientificState
            |
            v
    Future reasoning

instead of:

    Raw investigation
            |
            v
    Longer and longer conversation
