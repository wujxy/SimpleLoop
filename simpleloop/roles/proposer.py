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
from ..explore.models import ExploreReport
from ..explore.render import render_explore_for_state_header
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
    - ``"block"``:  the card failed an objective bar (``reason_kind`` +
      ``block_evidence_refs`` set).
    - ``"error"``:  the branch worker faulted (set by the orchestrator).
    """
    hypothesis: object  # HypothesisCard
    proposal: ResearchProposal | None = None
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
- {"action":"submit_proposals","proposals":[
    {"instruction":"...",
     "research_target":{"mode":"existing","finding_id":"F-NNN"},
     "evidence_refs":["source:src/foo.cc:FunctionName"],
     "material_difference":"..."}]}
  Submit ONE enriched proposal. The instruction MUST embed: (a) precise
  location (file / function / lines), (b) the code facts you read that motivate
  the change, (c) the correctness constraint the executor must preserve, and
  (d) which realization decisions you deliberately leave to the executor. Do
  NOT write the implementation, line-level code, or derived math.
  research_target declares an existing finding (mode=existing, F-NNN) or a new
  question (mode=new, with question/mechanisms/code_regions). evidence_refs and
  material_difference are optional. There is NO annotations field.
- {"action":"block","reason_kind":"false_claim|frozen|contradiction",
  "explanation":"...","evidence_refs":["source:src/foo.cc:FunctionName"]}
  Block ONLY for an objective failure, and EVERY block must cite at least one
  source: ref you read this round. reason_kind is one of:
    false_claim   — the card asserts a fact about the code that the code
                    refutes (cite the source:line that refutes it);
    frozen        — the only implementation site is under a frozen path
                    (cite it);
    contradiction — the card's own claims are mutually inconsistent (cite the
                    source that makes them so).
  You MUST NOT block because an idea is too hard, too risky, unlikely to work,
  too big a change, low ROI, or already tried — those are merit judgments
  reserved for the Harness. Wanting to block for any of those is a signal to
  enrich and submit_proposals instead.
"""

_RUNTIME_BOUNDARIES = """Runtime boundaries:
- /source is the accepted revision, /repo is its read-only Git repository,
  /history.jsonl and /rounds are persisted run evidence when present, and
  /scratch is temporary writable space.
- You cannot call the Executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
""".strip()

_PARTIAL_SUBMIT_REMINDER = (
    "Budget nearly exhausted. Submit your enriched proposal now — partial "
    "enrichment is acceptable. Do NOT block unless you have found an objective "
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
        cwd = action.get("cwd", "source")
        if not isinstance(command, str) or not command.strip():
            raise ProposerError("research command must be non-empty")
        if cwd not in {"source", "scratch"}:
            raise ProposerError("research cwd must be source or scratch")
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
    /source. The uniform objective choke point for every block."""
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

    def research_branch(
        self,
        *,
        hypothesis,  # HypothesisCard
        goal: str,
        editable: list[str],
        frozen: list[str],
        memory_service,
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None = None,
        explore: ExploreReport | None = None,
        max_steps: int | None = None,
        generator_regenerate=None,
    ) -> BranchResult:
        """Sieve + enrich one hypothesis in isolation.

        Reads the target site, blocks only on an objective bar failure (with a
        source ref), otherwise enriches the card into an executor-ready
        instruction and submits. Budget exhaustion submits partial — it never
        abandons an idea.
        """
        from .hypothesis import HypothesisCard  # avoid top-level cycle
        assert isinstance(hypothesis, HypothesisCard)

        system_prompt = (
            f"{load_semantic('proposer', prompt_dir).rstrip()}\n\n"
            f"{_runtime_protocol()}"
        )
        if explore is None:
            try:
                explore = memory_service.analyze_explore(
                    current_round=current_round)
            except Exception:
                explore = None
        startup_pack = memory_service.build_startup_pack(
            goal=goal, editable=editable, frozen=frozen,
            base_sha=base_sha, gate_block=gate_block,
            candidates_per_round=1, hints=hints,
            current_round=current_round, explore=explore,
        )
        branch_intro = (
            "You are researching ONE hypothesis in isolation. You are the "
            "cognitive element — NOT a reviewer. Do not judge whether the idea "
            "is worth trying; that is the Harness's job alone.\n\n"
            f"Hypothesis (from {hypothesis.generative_op}):\n"
            f"  region: {hypothesis.region}\n"
            f"  mechanism: {hypothesis.mechanism}\n"
            f"  intervention_family: {hypothesis.intervention_family}\n"
            f"  why_plausible: {hypothesis.why_plausible}\n"
            f"  critical_unknown: {hypothesis.critical_unknown}\n"
            f"  facts_read (from your Generator partner):\n"
            + "".join(f"    - {f}\n" for f in hypothesis.facts_read)
            + "\nFirst SIEVE: read just enough at the target site to confirm the "
            "three objective bars — the card's factual claims hold against the "
            "code, the site is not under a frozen path, and the card is "
            "self-consistent. The facts_read above are your partner's basis; "
            "verify they are true. If an objective bar fails, block with a "
            "source: ref. Then ENRICH: read until the executor can act without "
            "further codebase search, and submit_proposals with an enriched "
            "instruction. You are done only when you submit or block."
        )
        messages = [
            {"role": "user", "content": startup_pack},
            {"role": "user", "content": branch_intro},
        ]
        steps_budget = max_steps or self.max_steps
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        state = WorkingState()
        state.candidate_directions = (
            f"{hypothesis.mechanism} → {hypothesis.intervention_family} "
            f"in {hypothesis.region}")
        state.current_information_goal = hypothesis.critical_unknown
        budget_reminder_step = int(0.8 * steps_budget)
        reminded = False
        print(
            f"[branch {hypothesis.generative_op} "
            f"{hypothesis.signature()}] started max_steps={steps_budget}",
            flush=True,
        )
        with TemporaryDirectory(prefix="simpleloop-branch-") as scratch:
            tools = ResearchTools(
                runtime=self.runtime,
                source=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                memory_service=memory_service,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=current_round,
            )
            for _step_num in range(steps_budget):
                step = _step_num + 1
                print(f"[branch step {step}/{steps_budget}] thinking", flush=True)
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

                if name == "submit_proposals":
                    _bump(state, name)
                    print(
                        f"[branch] submit steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return BranchResult(
                        hypothesis=hypothesis,
                        proposal=action["proposals"][0],
                        outcome="submit",
                        usage=usages,
                        deliberation_telemetry=_build_telemetry(
                            state, steps=step, outcome="submit"),
                        trace=_build_trace(
                            state, round_id=current_round, outcome="submit",
                            explore=explore),
                    )
                if name == "block":
                    _bump(state, name)
                    print(
                        f"[branch] block steps={step} "
                        f"reason_kind={action['reason_kind']} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return BranchResult(
                        hypothesis=hypothesis,
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
                            evidence_refs=action["evidence_refs"], explore=explore),
                    )

                if name == "feedback_generator":
                    _bump(state, name)
                    if generator_regenerate is None:
                        raise ProposerError(
                            "feedback_generator issued but no "
                            "generator_regenerate callback was provided")
                    print(
                        f"[branch] feedback_generator steps={step}",
                        flush=True,
                    )
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
                    hypothesis = new_hypothesis
                    state.candidate_directions = (
                        f"{hypothesis.mechanism} → "
                        f"{hypothesis.intervention_family} "
                        f"in {hypothesis.region}")
                    state.current_information_goal = (
                        hypothesis.critical_unknown)
                    print(
                        f"[branch] generator regenerated "
                        f"{new_hypothesis.signature()} steps={step}",
                        flush=True,
                    )
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
                    f"[branch step {step}/{steps_budget}] "
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

            # Budget exhausted without a terminal action — submit partial.
            # Never abandon: the sieve job is bounded and a raw card is an
            # acceptable floor; the orchestrator deprioritizes thin proposals
            # by tool_calls and drops zero-read partials before execution.
            print(
                f"[branch] budget exhausted → partial submit (steps={steps_budget})",
                flush=True,
            )
            return self._partial_submit(
                hypothesis, state, usages, steps_budget, current_round, explore)

    def _partial_submit(self, hypothesis, state, usages, steps_budget,
                        current_round, explore):
        instruction = (
            "Enrichment incomplete (branch budget exhausted); the executor "
            "should locate and implement the hypothesis directly.\n"
            f"region: {hypothesis.region}\n"
            f"mechanism: {hypothesis.mechanism}\n"
            f"intervention_family: {hypothesis.intervention_family}\n"
            f"why_plausible: {hypothesis.why_plausible}\n"
            f"critical_unknown: {hypothesis.critical_unknown}\n"
        )
        proposal = ResearchProposal(
            instruction=instruction,
            research_target=NewFindingTarget(
                question=(hypothesis.why_plausible or hypothesis.mechanism)[:200],
            ),
        )
        return BranchResult(
            hypothesis=hypothesis,
            proposal=proposal,
            outcome="submit",
            enrichment_partial=True,
            usage=usages,
            deliberation_telemetry=_build_telemetry(
                state, steps=steps_budget, outcome="submit",
                enrichment_partial=True,
            ),
            trace=_build_trace(
                state, round_id=current_round, outcome="submit", explore=explore),
        )

