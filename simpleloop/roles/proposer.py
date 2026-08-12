"""Cognitive element: per-hypothesis Sieve + Enricher.

Each hypothesis card from the generator is researched in isolation by one agent
loop. The cognitive element is NOT a reviewer: it never judges whether an idea
is worth trying. Merit — will the change preserve correctness, will it be
faster — is structurally unknowable by an LLM in this domain and is reserved
for the Harness, which is the only source of truth. The cognitive element has
exactly two jobs:

- **Sieve (light):** read just enough at the target site to confirm the three
  objective bars. Block ONLY on an objective failure — a false factual claim
  about the code, a frozen-path conflict, or a self-contradiction — each backed
  by a ``source:`` ref read this round.
- **Enrich (deep):** rewrite the card into an executor-ready ``instruction``
  (precise location + the code facts read + the correctness constraint the
  executor must preserve + a declaration of which realization decisions are
  left to the executor), then ``submit``. It never writes the implementation,
  line-level code, or derived math.

This runtime never restricts the search space on a judgment. Submit is the
default terminal; block is rare and evidence-bound; budget exhaustion submits
partial (it never abandons an idea).

Doc reference: prompts/proposer.md.
Continuity across rounds is supplied by the persistent Experiment Ledger,
Finding Archive, and Frontier — NOT by this runtime's round-local state.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from .model import ChatModel
from .research_tools import (
    MEMORY_TOOL_ACTIONS,
    ResearchTools,
    render_research_tool_prompt,
)
from .research_agent import (
    AgentError,
    ResearchAgent,
    WorkingState,
    _bump,
    _build_telemetry,
    _build_trace,
    _fingerprint,
    _register_evidence,
    _render_state_header,
    _result_summary,
    _action_summary,
)
from ..container.runtime import ApptainerRuntime
from ..memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)
from ..explore import analyze_explore_from_schema
from ..explore.models import ExploreReport
from ..prompts import load_semantic


class ProposerError(AgentError):
    """The cognitive element violated its action or budget contract."""


@dataclass(frozen=True)
class ProposerResult:
    """One round's structured output.

    ``proposals`` is a list of ``ResearchProposal``. When no branch produced a
    proposal the orchestrator abstains (``abstained`` True, empty proposals).
    ``deliberation_telemetry`` carries behavioral facts for the round record;
    ``trace`` is the non-authoritative trajectory (never injected forward).
    """

    proposals: list[ResearchProposal]
    usage: object = None
    abstained: bool = False
    abstain_reason: str | None = None
    abstain_blocking_unknown: str | None = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BranchResult:
    """One hypothesis branch's outcome.

    ``outcome`` is one of:
    - ``"submit"``: the card was enriched into an executor-ready proposal
      (``proposal`` set; ``enrichment_partial`` True if the budget ran out
      mid-enrich and a partial instruction was submitted instead).
      In batch mode, ``proposals`` holds all K enriched proposals.
    - ``"block"``:  the card failed an objective bar (``reason_kind`` +
      ``block_evidence_refs`` set).
    - ``"error"``:  the branch worker faulted (set by the orchestrator).
    """
    hypothesis: object  # HypothesisCard
    proposal: ResearchProposal | None = None
    proposals: tuple[ResearchProposal, ...] = ()  # batch mode: K proposals
    outcome: str = "submit"
    reason_kind: str | None = None        # set on block
    explanation: str | None = None        # set on block
    block_evidence_refs: tuple[str, ...] = ()
    enrichment_partial: bool = False      # set on budget-exhaust submit
    usage: object = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


# --- Tunables (cognitive-specific) -----------------------------------------

_BLOCK_REASON_KINDS = frozenset({"false_claim", "frozen", "contradiction"})

# Research / memory tools never terminate the loop.
_RESEARCH_TOOL_ACTIONS = frozenset(
    {"run_research_command"} | MEMORY_TOOL_ACTIONS
)


# --- Prompt scaffolding ----------------------------------------------------

_PROTOCOL_ENVELOPE = (
    "Runtime contract (immutable):\n"
    "Return exactly one JSON object per response, with no prose outside it."
)

_RESEARCH_PHASE_NOTE = (
    "Research tools (use freely to locate and read the target):\n"
    + render_research_tool_prompt()
)

_PROTOCOL_BLOCK = """Control actions (you are done only when you submit or block):
- {"action":"select_for_enrich","selected":[
    {"hypothesis_idx":0,"slot":"hotspot|new_direction",
     "evidence_refs":["experiment:r0c0"],"rationale":"..."}]}
  Declare which hypotheses you will enrich. This sets the scope for your
  enrichment work, so submit_proposals can follow it. Each selection needs
  evidence_refs (history + source reads) and a rationale.
- {"action":"submit_proposals","proposals":[
    {"instruction":"...",
     "research_target":{"mode":"existing","finding_id":"F-NNN"},
     "evidence_refs":["source:src/foo.cc:FunctionName"],
     "material_difference":"..."}]}
  Submit enriched proposals — one per hypothesis you selected. The instruction
  embeds: (a) location at the function/class level, (b) the code facts you
  read that motivate the change, (c) the correctness constraint the executor
  preserves, and (d) which realization decisions you leave to the executor.
  research_target declares an existing finding (mode=existing, F-NNN) or a
  new question (mode=new, with question/mechanisms/code_regions).
  evidence_refs and material_difference are optional. There is NO annotations
  field.
- {"action":"block","reason_kind":"false_claim|frozen|contradiction",
  "explanation":"...","evidence_refs":["source:src/foo.cc:FunctionName"]}
  Block only for an objective failure, citing at least one source: ref you
  read this round. reason_kind is one of:
    false_claim   — the card asserts a fact about the code that the code
                    refutes (cite the source:line that refutes it);
    frozen        — the only implementation site is under a frozen path
                    (cite it);
    contradiction — the card's own claims are mutually inconsistent (cite the
                    source that makes them so).
  "Too hard", "too risky", "unlikely to work", "low ROI", or "already tried"
  are merit judgments reserved for the Harness. If you catch yourself wanting
  to block for one of those, enrich and submit_proposals instead — let the
  Harness decide.
"""

_RUNTIME_BOUNDARIES = """Runtime boundaries:
- /work is your writable lab: the accepted source tree's editable paths,
  materialized read-write. Read it, write scratch code, compile, run toy
  experiments to understand the code and the task. It is disposable — nothing
  you write here becomes an artifact. Other source files (tests, build files)
  are visible read-only.
- /repo is the read-only Git repository. Use `git show <sha>`, `git diff`,
  `git log` to inspect any prior experiment's source (the history is shared).
  You CANNOT commit, branch, or reset — creating artifacts is the candidate's
  job, and the read-only /repo structurally prevents it.
- /history.jsonl and /rounds are persisted run evidence when present;
  /scratch is temporary writable space.
- Anything you measure in your lab (a toy build, a probe) is for YOUR
  understanding only. It is never a merit fact: whether a change is faster or
  correct is the Harness's verdict, not yours.
- You cannot call the Executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
""".strip()

_PARTIAL_SUBMIT_REMINDER = (
    "Budget nearly exhausted. Submit your enriched proposal now — partial "
    "enrichment is acceptable. Block only if you have found an objective "
    "source conflict and can cite a source: ref for it. Return exactly one "
    "JSON action object."
)


def _runtime_protocol() -> str:
    return "\n\n".join((
        _PROTOCOL_ENVELOPE,
        _RESEARCH_PHASE_NOTE,
        _PROTOCOL_BLOCK,
        _RUNTIME_BOUNDARIES,
    ))


# --- Guard repair messages -------------------------------------------------

_GUARD_REASONS = {
    "repeated_tool": (
        "That tool call is identical to the previous one and would add no new "
        "information. Change the query, inspect a different region, or move on "
        "via submit_proposals or block."
    ),
    "block_needs_source": (
        "A block must cite at least one source: ref to a path you actually read "
        "this round. If you have no such evidence, you cannot block — enrich "
        "and submit_proposals instead."
    ),
}


def _guard_repair_message(reason: str) -> str:
    base = _GUARD_REASONS.get(reason, "")
    return (
        f"Protocol correction required ({reason}). {base} Return exactly one "
        "JSON action object matching the Runtime contract, with no prose or "
        "additional JSON."
    )


# --- Action parsing -------------------------------------------------------

def _require_keys(
    value: dict,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    if not required <= set(value) or set(value) - allowed:
        raise ProposerError(
            f"invalid keys for {value.get('action')}: {sorted(value)}"
        )


def _require_string_list(value, *, name: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ProposerError(f"{name} must be a list")
    if not allow_empty and not value:
        raise ProposerError(f"{name} must be non-empty")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProposerError(f"{name} must contain non-empty strings")
        out.append(item.strip())
    return out


def _parse_research_target(value) -> ExistingFindingTarget | NewFindingTarget:
    if not isinstance(value, dict):
        raise ProposerError("research_target must be an object")
    mode = value.get("mode")
    if mode == "existing":
        if set(value) - {"mode", "finding_id"}:
            raise ProposerError(
                f"research_target(existing) has unexpected keys: {sorted(value)}"
            )
        finding_id = value.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ProposerError(
                "research_target(existing).finding_id must be a non-empty string"
            )
        return ExistingFindingTarget(finding_id=finding_id.strip())
    if mode == "new":
        allowed = {"mode", "question", "mechanisms", "code_regions"}
        if set(value) - allowed:
            raise ProposerError(
                f"research_target(new) has unexpected keys: {sorted(value)}"
            )
        question = value.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ProposerError(
                "research_target(new).question must be a non-empty string"
            )
        mechanisms = tuple(_require_string_list(
            value.get("mechanisms", []),
            name="research_target.mechanisms", allow_empty=True,
        ))
        code_regions = tuple(_require_string_list(
            value.get("code_regions", []),
            name="research_target.code_regions", allow_empty=True,
        ))
        return NewFindingTarget(
            question=question.strip(),
            mechanisms=mechanisms,
            code_regions=code_regions,
        )
    raise ProposerError(
        f"research_target.mode must be 'existing' or 'new', got {mode!r}"
    )


def _parse_proposal(value) -> ResearchProposal:
    if not isinstance(value, dict):
        raise ProposerError("proposal must be an object")
    allowed = {
        "instruction", "research_target", "evidence_refs", "material_difference",
    }
    if set(value) - allowed:
        raise ProposerError(f"proposal has unexpected keys: {sorted(value)}")
    instruction = value.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ProposerError("proposal.instruction must be non-empty")
    target = _parse_research_target(value.get("research_target"))
    evidence_refs = tuple(_require_string_list(
        value.get("evidence_refs", []),
        name="proposal.evidence_refs", allow_empty=True,
    ))
    md = value.get("material_difference")
    if md is not None and (not isinstance(md, str) or not md.strip()):
        raise ProposerError(
            "proposal.material_difference must be a non-empty string when present"
        )
    return ResearchProposal(
        instruction=instruction.strip(),
        research_target=target,
        evidence_refs=evidence_refs,
        material_difference=(md.strip() if isinstance(md, str) else None),
    )


def _opt_str(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProposerError("expected a string field")
    return value.strip()


def _parse_action(text: str, candidates_per_round: int) -> dict:
    try:
        action = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProposerError("proposer response must be one JSON object") from exc
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise ProposerError("proposer action must be a JSON object with action")
    name = action["action"]

    # --- research / memory tools (never terminate) ---
    if name == "run_research_command":
        _require_keys(action, {"action", "command"}, {"cwd"})
        command = action["command"]
        cwd = action.get("cwd", "work")
        if not isinstance(command, str) or not command.strip():
            raise ProposerError("research command must be non-empty")
        if cwd not in {"work", "scratch"}:
            raise ProposerError("research cwd must be work or scratch")
        return {"action": name, "command": command, "cwd": cwd}
    if name == "inspect_episode":
        _require_keys(action, {"action", "ref"})
        ref = action["ref"]
        if not isinstance(ref, str) or not ref.strip():
            raise ProposerError("episode ref must be non-empty")
        return {"action": name, "ref": ref.strip()}
    if name == "list_findings":
        _require_keys(action, {"action"}, {"state", "limit"})
        state = action.get("state", "active")
        if state not in {"active", "open", "dormant", "archived", "all"}:
            raise ProposerError(
                "list_findings.state must be one of active/open/dormant/"
                "archived/all"
            )
        limit = action.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError("list_findings.limit must be a positive integer")
        return {"action": name, "state": state, "limit": limit}
    if name == "search_findings":
        _require_keys(action, {"action", "query"}, {"limit"})
        query = action["query"]
        if not isinstance(query, str) or not query.strip():
            raise ProposerError("search_findings.query must be non-empty")
        limit = action.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError(
                "search_findings.limit must be a positive integer"
            )
        return {"action": name, "query": query.strip(), "limit": limit}
    if name == "inspect_finding":
        _require_keys(action, {"action", "finding_id"})
        fid = action["finding_id"]
        if not isinstance(fid, str) or not fid.strip():
            raise ProposerError("inspect_finding.finding_id must be non-empty")
        return {"action": name, "finding_id": fid.strip()}
    if name == "search_experiments":
        _require_keys(
            action, {"action", "query"},
            {"filters", "limit", "buckets"},
        )
        query = action["query"]
        if not isinstance(query, str) or not query.strip():
            raise ProposerError("search_experiments.query must be non-empty")
        filters = action.get("filters")
        if filters is not None and not isinstance(filters, dict):
            raise ProposerError("search_experiments.filters must be an object")
        allowed_filters = {
            "gate_passed", "eligible", "selected", "finding_id",
            "changed_path", "round_min", "round_max", "status",
        }
        if filters:
            unknown = set(filters) - allowed_filters
            if unknown:
                raise ProposerError(
                    f"search_experiments.filters has unknown keys: {sorted(unknown)}"
                )
        limit = action.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError(
                "search_experiments.limit must be a positive integer"
            )
        buckets = action.get("buckets", True)
        if not isinstance(buckets, bool):
            raise ProposerError("search_experiments.buckets must be a bool")
        return {
            "action": name, "query": query.strip(),
            "filters": filters or {}, "limit": limit, "buckets": buckets,
        }

    # --- feedback to Generator (non-terminal, triggers regeneration) ---
    if name == "feedback_generator":
        _require_keys(
            action,
            {"action", "evidence_refs", "observation",
             "relation_to_seed", "implication"},
        )
        refs = _require_string_list(
            action["evidence_refs"], name="feedback_generator.evidence_refs",
        )
        observation = action["observation"]
        if not isinstance(observation, str) or not observation.strip():
            raise ProposerError(
                "feedback_generator.observation must be a non-empty string")
        relation = action["relation_to_seed"]
        if not isinstance(relation, str) or not relation.strip():
            raise ProposerError(
                "feedback_generator.relation_to_seed must be non-empty")
        implication = action["implication"]
        if not isinstance(implication, str) or not implication.strip():
            raise ProposerError(
                "feedback_generator.implication must be a non-empty string")
        return {
            "action": name,
            "evidence_refs": tuple(refs),
            "observation": observation.strip(),
            "relation_to_seed": relation.strip(),
            "implication": implication.strip(),
        }

    # --- control actions ---
    if name == "select_for_enrich":
        _require_keys(action, {"action", "selected"})
        selected = action["selected"]
        if not isinstance(selected, list) or not selected:
            raise ProposerError("select_for_enrich.selected must be non-empty")
        parsed_sel = []
        for item in selected:
            if not isinstance(item, dict):
                raise ProposerError("select_for_enrich items must be objects")
            _require_keys(item, {"hypothesis_idx", "slot",
                                 "evidence_refs", "rationale"})
            idx = item["hypothesis_idx"]
            if not isinstance(idx, int) or idx < 0:
                raise ProposerError(
                    "select_for_enrich.hypothesis_idx must be a non-negative int")
            slot = item["slot"]
            if not isinstance(slot, str) or not slot.strip():
                raise ProposerError(
                    "select_for_enrich.slot must be a non-empty string")
            refs = _require_string_list(
                item["evidence_refs"], name="select_for_enrich.evidence_refs")
            rationale = item["rationale"]
            if not isinstance(rationale, str) or not rationale.strip():
                raise ProposerError(
                    "select_for_enrich.rationale must be non-empty")
            parsed_sel.append({
                "hypothesis_idx": idx,
                "slot": slot.strip(),
                "evidence_refs": tuple(refs),
                "rationale": rationale.strip(),
            })
        return {"action": name, "selected": parsed_sel}

    if name == "submit_proposals":
        _require_keys(action, {"action", "proposals"})
        proposals = action["proposals"]
        if (not isinstance(proposals, list)
                or not 1 <= len(proposals) <= candidates_per_round):
            raise ProposerError(
                f"expected 1..{candidates_per_round} proposals"
            )
        parsed = [_parse_proposal(item) for item in proposals]
        return {"action": name, "proposals": parsed}

    if name == "block":
        _require_keys(
            action, {"action", "reason_kind", "explanation", "evidence_refs"})
        rk = action["reason_kind"]
        if rk not in _BLOCK_REASON_KINDS:
            raise ProposerError(
                f"block.reason_kind must be one of {sorted(_BLOCK_REASON_KINDS)}, "
                f"got {rk!r}"
            )
        explanation = action["explanation"]
        if not isinstance(explanation, str) or not explanation.strip():
            raise ProposerError("block.explanation must be non-empty")
        refs = _require_string_list(
            action["evidence_refs"], name="block.evidence_refs",
        )
        return {
            "action": name,
            "reason_kind": rk,
            "explanation": explanation.strip(),
            "evidence_refs": tuple(refs),
        }

    raise ProposerError(f"unknown proposer action: {name}")


# --- Cognitive-specific guards -------------------------------------------

from .research_agent import _source_path_exists  # noqa: E402


def _validate_block_evidence(
    refs, state: WorkingState, source_root: Path,
) -> bool:
    """True when the block cites at least one ``source:`` ref to a path the
    agent read this branch (present in ``new_evidence``) that exists under
    the workspace. The uniform objective choke point for every block."""
    for ref in refs:
        if ":" not in ref:
            continue
        kind, _, rest = ref.partition(":")
        if kind == "source":
            if ("__source_examined__" in state.new_evidence
                    and _source_path_exists(rest, source_root)):
                return True
    return False


def _validate_action_guard(
    state: WorkingState, action: dict, source_root: Path,
) -> str | None:
    """Return a repair reason, or None when the action satisfies the guards."""
    name = action["action"]
    if name in _RESEARCH_TOOL_ACTIONS:
        # Exact-repeat only: an identical tool call back-to-back adds nothing.
        fp = _fingerprint(action)
        if (state.last_tool_fingerprint is not None
                and fp == state.last_tool_fingerprint):
            return "repeated_tool"
        return None
    if name == "block":
        if not _validate_block_evidence(action["evidence_refs"], state, source_root):
            return "block_needs_source"
        return None
    # submit_proposals: no guard — merit is not the cognitive element's job.
    return None


# --- The cognitive element ------------------------------------------------

class ProposerAgent(ResearchAgent):
    _error_class = ProposerError

    def __init__(
        self,
        *,
        model: ChatModel,
        runtime: ApptainerRuntime,
        timeout_seconds: int,
        max_steps: int,
        command_timeout_seconds: int,
        command_output_cap_chars: int,
        usage_observer=None,
    ):
        super().__init__(
            model=model, runtime=runtime,
            timeout_seconds=timeout_seconds, max_steps=max_steps,
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
        )
        self._candidates_per_round = 1

    def _parse_action(self, text: str) -> dict:
        return _parse_action(text, self._candidates_per_round)

    def _validate_guard(
        self, state: WorkingState, action: dict, source_root: Path,
    ) -> str | None:
        return _validate_action_guard(state, action, source_root)

    def research_batch(
        self,
        *,
        hypotheses: list,  # list[HypothesisCard]
        select_quota: int,
        goal: str,
        editable: list[str],
        frozen: list[str],
        world_mount,
        memory_service,
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None = None,
        explore=None,
        max_steps: int | None = None,
        generator_regenerate=None,
    ) -> BranchResult:
        """Batch audit: sieve + dedup + select up to K + enrich selections.

        All seed hypotheses from the generator are audited in one context.
        The cognitive element selects up to ``select_quota`` for enrichment with
        evidence-based rationale, enriches each, and submits all K as
        proposals. This is NOT merit judgment — selection is based on
        historical coverage facts (ledger) and source facts, not predictions.
        """
        from .hypothesis import HypothesisCard, dedup_by_signature
        assert all(isinstance(h, HypothesisCard) for h in hypotheses)
        assert select_quota >= 1
        self._candidates_per_round = select_quota

        # Dedup by signature before presenting to the cognitive element.
        deduped = dedup_by_signature(hypotheses, per_bin=1)
        if not deduped:
            return BranchResult(
                hypothesis=hypotheses[0] if hypotheses else None,
                outcome="block",
                reason_kind="contradiction",
                explanation="all hypotheses deduplicated to nothing",
                block_evidence_refs=(),
            )

        system_prompt = (
            f"{load_semantic('proposer', prompt_dir).rstrip()}\n\n"
            f"{_runtime_protocol()}"
        )
        if explore is None:
            try:
                explore = analyze_explore_from_schema(
                    memory_service.load_findings(),
                    memory_service.load_experiments(),
                    current_round=current_round,
                    metrics_schema=memory_service.metrics_schema,
                )
            except Exception:
                explore = None
        startup_pack = memory_service.build_startup_pack(
            goal=goal, editable=editable, frozen=frozen,
            base_sha=base_sha, gate_block=gate_block,
            candidates_per_round=select_quota, hints=hints,
            current_round=current_round, explore=explore,
        )

        # Build the batch intro listing all deduped hypotheses.
        hyp_lines = []
        for i, h in enumerate(deduped):
            hyp_lines.append(
                f"  [{i}] (lens {h.generative_op})\n"
                f"      region: {h.region}\n"
                f"      mechanism: {h.mechanism}\n"
                f"      intervention_family: {h.intervention_family}\n"
                f"      why_plausible: {h.why_plausible}\n"
                f"      critical_unknown: {h.critical_unknown}\n"
                f"      facts_read:\n"
                + "".join(f"        - {f}\n" for f in h.facts_read)
            )
        batch_intro = (
            f"You are auditing {len(deduped)} seed hypotheses from your "
            f"Generator partner, listed below. You have the experiment "
            f"ledger and explore health in your startup context.\n\n"
            f"## Hypotheses\n\n"
            + "\n".join(hyp_lines)
            + f"\n## Protocol: batch audit\n\n"
            f"The work has three phases, each feeding the next.\n\n"
            f"1. **Sieve:** check each hypothesis against the three objective "
            f"bars — factual claims hold, site not frozen, self-consistent. "
            f"You may read source to verify. This filters out leads that are "
            f"factually wrong before you invest in enrichment.\n"
            f"2. **Select:** your partner gave you leads, not plans. Choose "
            f"up to {select_quota} worth pursuing via `select_for_enrich`, each "
            f"with `evidence_refs` and a `rationale` grounded in facts. "
            f"Prefer materially different mechanisms or code regions when "
            f"useful, but this is a semantic preference, not a uniqueness "
            f"gate. Do not regenerate merely to fill the limit.\n"
            f"3. **Enrich:** for each selected lead, read the actual "
            f"implementation until you understand the function and class "
            f"structure, the code facts, and the correctness constraints. "
            f"Then `submit_proposals` with one proposal per selected lead. "
            f"A proposal is a scheme direction between the lead and the "
            f"implementation: locate at function/class level, state the "
            f"constraint, declare what is left open. The executor reads the "
            f"real source and makes the concrete changes.\n\n"
            f"You are still NOT a reviewer. 'This will be faster' is a "
            f"prediction — leave it to the Harness. 'This region has 0 "
            f"prior attempts' is a fact. 'This family improved last round' "
            f"is a fact. Select on facts, not predictions. You are done "
            f"when you submit_proposals or block (block only if ALL "
            f"hypotheses fail an objective bar)."
        )

        messages = [
            {"role": "user", "content": startup_pack},
            {"role": "user", "content": batch_intro},
        ]
        steps_budget = max_steps or self.max_steps
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        state = WorkingState()
        state.candidate_directions = (
            f"batch audit of {len(deduped)} hypotheses, select {select_quota}")
        budget_reminder_step = int(0.8 * steps_budget)
        reminded = False
        print(
            f"[batch] {len(deduped)} hypotheses, select_quota={select_quota} "
            f"max_steps={steps_budget}",
            flush=True,
        )

        selected_indices: list[int] = []
        selected_rationales: list[str] = []
        all_proposals: list[ResearchProposal] = []
        has_selected = False

        with TemporaryDirectory(prefix="simpleloop-batch-") as scratch, \
                TemporaryDirectory(prefix="simpleloop-session-") as session_root:
            home = Path(session_root) / "home"
            home.mkdir(mode=0o700)
            tools = ResearchTools(
                runtime=self.runtime,
                workspace=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                world_mount=world_mount,
                home=home,
                memory_service=memory_service,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=current_round,
            )
            for _step_num in range(steps_budget):
                step = _step_num + 1
                print(f"[batch step {step}/{steps_budget}] thinking", flush=True)
                if (not reminded and budget_reminder_step > 0
                        and step >= budget_reminder_step):
                    messages.append({
                        "role": "user", "content": _PARTIAL_SUBMIT_REMINDER,
                    })
                    reminded = True
                action, reply_text = self._step(
                    state, messages, system_prompt, deadline, usages, step,
                    source_root=source_path, steps_budget=steps_budget,
                )
                name = action["action"]
                state.action_log.append({"action": name, "step": step})

                if name == "select_for_enrich":
                    _bump(state, name)
                    new_sel = action["selected"]
                    if len(selected_indices) + len(new_sel) > select_quota:
                        state.protocol_repairs += 1
                        print(
                            f"[batch step {step}/{steps_budget}] "
                            f"select exceeds quota: have {len(selected_indices)}, "
                            f"adding {len(new_sel)}, quota {select_quota}",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": (
                                f"You have selected {len(selected_indices)} "
                                f"hypothesis(es) and are adding {len(new_sel)}, "
                                f"but the limit is {select_quota}. Select at "
                                f"most {select_quota} total. "
                                f"Return exactly one JSON action object."
                            )},
                        ])
                        continue
                    for sel in new_sel:
                        idx = sel["hypothesis_idx"]
                        if idx >= len(deduped):
                            raise ProposerError(
                                f"select_for_enrich idx {idx} out of range "
                                f"(have {len(deduped)} hypotheses)")
                        selected_indices.append(idx)
                        selected_rationales.append(sel["rationale"])
                    has_selected = True
                    print(
                        f"[batch] selected {selected_indices} "
                        f"steps={step}",
                        flush=True,
                    )
                    # Prompt to start enriching the selected hypotheses.
                    sel_cards = [deduped[i] for i in selected_indices]
                    enrich_prompt = (
                        f"Selected {len(sel_cards)} hypothesis/hypotheses. "
                        f"Now enrich each one: read the target site until "
                        f"the executor can act without further codebase "
                        f"search, then submit_proposals with "
                        f"{len(sel_cards)} enriched proposals — one per "
                        f"selected hypothesis."
                    )
                    for i, h in enumerate(sel_cards):
                        enrich_prompt += (
                            f"\n\nHypothesis {i}: "
                            f"region={h.region} mechanism={h.mechanism} "
                            f"intervention_family={h.intervention_family}")
                    messages.extend([
                        {"role": "assistant", "content": reply_text},
                        {"role": "user", "content": enrich_prompt},
                    ])
                    continue

                if name == "submit_proposals":
                    if not has_selected:
                        state.protocol_repairs += 1
                        print(
                            f"[batch step {step}/{steps_budget}] "
                            f"submit without select -- rejected",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": (
                                "You submitted proposals before calling "
                                "select_for_enrich. Your partner gave you "
                                f"{len(deduped)} leads; select_for_enrich "
                                f"declares which {select_quota} you are "
                                "taking on, and sets the scope for your "
                                "enrichment. Select first, then enrich and "
                                "submit. Return exactly one JSON action object."
                            )},
                        ])
                        continue
                    if len(action["proposals"]) != len(selected_indices):
                        state.protocol_repairs += 1
                        print(
                            f"[batch step {step}/{steps_budget}] "
                            f"submit count {len(action['proposals'])} != "
                            f"selected {len(selected_indices)} -- rejected",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": (
                                f"You selected {len(selected_indices)} "
                                f"hypothesis(es) but submitted "
                                f"{len(action['proposals'])} proposal(s). "
                                f"Each selected hypothesis gets one proposal, "
                                f"so submit {len(selected_indices)} total. "
                                "Return exactly one JSON action object."
                            )},
                        ])
                        continue
                    _bump(state, name)
                    all_proposals = action["proposals"]
                    print(
                        f"[batch] submit {len(all_proposals)} proposal(s) "
                        f"steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return BranchResult(
                        hypothesis=deduped[selected_indices[0]]
                        if selected_indices else deduped[0],
                        proposal=all_proposals[0],
                        proposals=tuple(all_proposals),
                        outcome="submit",
                        usage=usages,
                        deliberation_telemetry=_build_telemetry(
                            state, steps=step, outcome="submit"),
                        trace=_build_trace(
                            state, round_id=current_round,
                            outcome="submit", explore=explore),
                    )

                if name == "block":
                    _bump(state, name)
                    print(
                        f"[batch] block steps={step} "
                        f"reason_kind={action['reason_kind']} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return BranchResult(
                        hypothesis=deduped[0],
                        outcome="block",
                        reason_kind=action["reason_kind"],
                        explanation=action["explanation"],
                        block_evidence_refs=action["evidence_refs"],
                        usage=usages,
                        deliberation_telemetry=_build_telemetry(
                            state, steps=step, outcome="block",
                            reason_kind=action["reason_kind"]),
                        trace=_build_trace(
                            state, round_id=current_round, outcome="block",
                            reason_kind=action["reason_kind"],
                            evidence_refs=action["evidence_refs"],
                            explore=explore),
                    )

                if name == "feedback_generator":
                    _bump(state, name)
                    if generator_regenerate is None:
                        raise ProposerError(
                            "feedback_generator issued but no "
                            "generator_regenerate callback was provided")
                    print(f"[batch] feedback_generator steps={step}", flush=True)
                    new_hypothesis = generator_regenerate(action)
                    envelope = {
                        "state": _render_state_header(state, explore),
                        "feedback": action,
                        "new_hypothesis": {
                            "generative_op": new_hypothesis.generative_op,
                            "region": new_hypothesis.region,
                            "mechanism": new_hypothesis.mechanism,
                            "intervention_family":
                                new_hypothesis.intervention_family,
                            "why_plausible": new_hypothesis.why_plausible,
                            "critical_unknown": new_hypothesis.critical_unknown,
                        },
                    }
                    messages.extend([
                        {"role": "assistant", "content": reply_text},
                        {"role": "user", "content": json.dumps(
                            envelope, ensure_ascii=False,
                        )},
                    ])
                    continue

                # tool call
                observation = tools.execute(action, deadline=deadline)
                _bump(state, "tool")
                _register_evidence(state, action, observation)
                state.last_tool_fingerprint = _fingerprint(action)
                if observation.get("ok") and name == "run_research_command":
                    _bump(state, "source_read")
                    state.located = True
                print(
                    f"[batch step {step}/{steps_budget}] "
                    f"{_result_summary(action, observation)}",
                    flush=True,
                )
                envelope = {
                    "state": _render_state_header(state, explore),
                    "tool_result": observation,
                }
                messages.extend([
                    {"role": "assistant", "content": reply_text},
                    {"role": "user", "content": json.dumps(
                        envelope, ensure_ascii=False,
                    )},
                ])

            # Budget exhausted — partial submit if we have any proposals.
            if all_proposals:
                print(
                    f"[batch] budget exhausted with {len(all_proposals)} "
                    f"proposals — returning partial",
                    flush=True,
                )
                return BranchResult(
                    hypothesis=deduped[selected_indices[0]]
                    if selected_indices else deduped[0],
                    proposal=all_proposals[0],
                    proposals=tuple(all_proposals),
                    outcome="submit",
                    enrichment_partial=True,
                    usage=usages,
                    deliberation_telemetry=_build_telemetry(
                        state, steps=steps_budget, outcome="submit",
                        enrichment_partial=True),
                    trace=_build_trace(
                        state, round_id=current_round, outcome="submit",
                        explore=explore),
                )
            # No proposals — partial submit from selected hypotheses.
            if selected_indices:
                print(
                    f"[batch] budget exhausted after select, before enrich "
                    f"— partial submit for {len(selected_indices)} selected",
                    flush=True,
                )
                partial_proposals = []
                for idx in selected_indices:
                    h = deduped[idx]
                    partial_proposals.append(ResearchProposal(
                        instruction=(
                            "Enrichment incomplete (batch budget exhausted); "
                            "the executor should locate and implement the "
                            "hypothesis directly.\n"
                            f"region: {h.region}\n"
                            f"mechanism: {h.mechanism}\n"
                            f"intervention_family: {h.intervention_family}\n"
                            f"why_plausible: {h.why_plausible}\n"
                            f"critical_unknown: {h.critical_unknown}\n"
                        ),
                        research_target=NewFindingTarget(
                            question=(h.why_plausible or h.mechanism)[:200],
                        ),
                    ))
                return BranchResult(
                    hypothesis=deduped[selected_indices[0]],
                    proposal=partial_proposals[0],
                    proposals=tuple(partial_proposals),
                    outcome="submit",
                    enrichment_partial=True,
                    usage=usages,
                    deliberation_telemetry=_build_telemetry(
                        state, steps=steps_budget, outcome="submit",
                        enrichment_partial=True),
                    trace=_build_trace(
                        state, round_id=current_round, outcome="submit",
                        explore=explore),
                )
            # Nothing selected at all — block.
            return BranchResult(
                hypothesis=deduped[0],
                outcome="block",
                reason_kind="contradiction",
                explanation="batch budget exhausted before any selection",
                block_evidence_refs=(),
                usage=usages,
                deliberation_telemetry=_build_telemetry(
                    state, steps=steps_budget, outcome="block",
                    reason_kind="contradiction"),
                trace=_build_trace(
                    state, round_id=current_round, outcome="block",
                    reason_kind="contradiction", explore=explore),
            )
