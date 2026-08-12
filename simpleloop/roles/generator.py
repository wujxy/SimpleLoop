"""Generator: a lever-space surveyor that produces grounded hypothesis cards.

The Generator builds a factual basis of the task's subject matter (survey),
synthesizes a lever map, and diverges across the map to produce unverified
leads. It sees NO history -- no dashboard, no frontier, no exhausted-region
list, no prior outcomes. It reasons from the objective, the subject matter it
surveys, and a 5-of-9 generative-op subset.

The bound Cognitive element is its partner -- it audits the seed against
history the Generator can't see, enriches it into a proposal, or feeds history
back for regeneration.

The harness enforces two prerequisite couplings (survey before map, map before
submit) as a structural backstop. Everything else -- whether the survey was
deep enough, whether the map reflects the survey, whether the hypothesis is
grounded -- is the LLM's own responsibility. The harness does not verify
fields, match paths, or check references. See prompts/generator.md.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from .model import ChatModel, ModelError, ModelReply
from .hypothesis import HypothesisCard
from .research_agent import (
    AgentError,
    ResearchAgent,
    WorkingState,
    _bump,
    _register_evidence,
    _render_state_header,
    _result_summary,
)
from .research_tools import ResearchTools, render_research_tool_prompt
from ..container.runtime import ApptainerRuntime
from ..prompts import load_semantic


class GeneratorError(AgentError):
    """The Generator violated its action or budget contract."""


# How many G operations to spread across (the prompt defines G1-G9).
_GENERATIVE_OPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9")


@dataclass(frozen=True)
class GenerationResult:
    cards: list[HypothesisCard]
    usage: object = None


def _g_definition(semantic: str, op: str) -> str:
    """Extract one G's definition paragraph from the generator prompt text."""
    paragraph: list[str] = []
    capturing = False
    for line in semantic.splitlines():
        token = line.split()[0] if line.split() else ""
        is_header = token in _GENERATIVE_OPS and " — " in line
        if is_header:
            capturing = token == op
            if capturing:
                paragraph = [line]
            continue
        if capturing:
            if not line.strip():
                break
            paragraph.append(line)
    return "\n".join(paragraph)


def _replace_basis(semantic: str, ops: tuple[str, ...]) -> str:
    """Swap the full G1-G9 basis in the prompt for a scheduler-selected subset."""
    lines = semantic.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("## The Generative Basis"):
            header_idx = i
            break
    if header_idx is None:
        return semantic
    end_idx = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if lines[j].startswith("You are not required to use every G"):
            end_idx = j
            break
    defs = [_g_definition(semantic, op) for op in ops]
    defs = [d for d in defs if d]
    if not defs:
        return semantic
    new_body = "\n\n".join(defs)
    rebuilt = lines[:header_idx + 1] + ["", new_body, ""] + lines[end_idx:]
    return "\n".join(rebuilt)


# --- Hypothesis card parsing -----------------------------------------------

def _parse_hypothesis_card(item: dict) -> HypothesisCard | None:
    """Parse one hypothesis dict from submit_hypothesis. Returns None if the
    card is empty in all three structural fields.

    ``facts_read`` is required: non-empty list of non-empty strings. Each
    string is a factual observation from the survey. Missing/empty facts_read
    -> GeneratorError.
    """
    if not isinstance(item, dict):
        return None
    op = str(item.get("generative_op", "G6")).strip()
    if op not in _GENERATIVE_OPS:
        op = "G6"
    region = str(item.get("region", "")).strip()
    mechanism = str(item.get("mechanism", "")).strip()
    intervention = str(item.get("intervention_family", "")).strip()
    why = str(item.get("why_plausible", "")).strip()
    unknown = str(item.get("critical_unknown", "")).strip()
    if not region and not mechanism and not intervention:
        return None
    raw_facts = item.get("facts_read")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise GeneratorError(
            "submit_hypothesis.hypothesis.facts_read must be a non-empty "
            "list of factual observations from your survey"
        )
    facts: list[str] = []
    for f in raw_facts:
        if not isinstance(f, str) or not f.strip():
            raise GeneratorError(
                "facts_read entries must be non-empty strings"
            )
        facts.append(f.strip())
    return HypothesisCard(
        generative_op=op, region=region, mechanism=mechanism,
        intervention_family=intervention, why_plausible=why,
        critical_unknown=unknown, facts_read=tuple(facts),
    )


# --- Lever map parsing -----------------------------------------------------

def _parse_lever_map(item: dict) -> list[dict]:
    """Parse the levers list from emit_lever_map.

    Each lever: {part, role, structural_space}. All fields are non-empty
    free-form strings. No machine verification of content -- the LLM is
    responsible for whether the map reflects its survey.
    """
    if not isinstance(item, list) or not item:
        raise GeneratorError(
            "emit_lever_map.levers must be a non-empty list"
        )
    levers: list[dict] = []
    for entry in item:
        if not isinstance(entry, dict):
            raise GeneratorError("lever entries must be objects")
        part = entry.get("part")
        role = entry.get("role")
        space = entry.get("structural_space")
        for val, name in (
            (part, "part"), (role, "role"),
            (space, "structural_space"),
        ):
            if not isinstance(val, str) or not val.strip():
                raise GeneratorError(
                    f"lever.{name} must be a non-empty string"
                )
        levers.append({
            "part": part.strip(),
            "role": role.strip(),
            "structural_space": space.strip(),
        })
    return levers


# --- Generator action parsing ----------------------------------------------

def _parse_generator_action(text: str) -> dict:
    """Parse one action from the Generator's model reply.

    Phase action: emit_lever_map (lever map synthesis).
    Terminal: submit_hypothesis (hypothesis emit).
    Non-terminal: run_research_command (survey).
    """
    try:
        action = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GeneratorError("generator response must be one JSON object") from exc
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise GeneratorError("generator action must be a JSON object with action")
    name = action["action"]

    if name == "run_research_command":
        command = action.get("command")
        cwd = action.get("cwd", "work")
        if not isinstance(command, str) or not command.strip():
            raise GeneratorError("research command must be non-empty")
        if cwd not in {"work", "scratch"}:
            raise GeneratorError("research cwd must be work or scratch")
        return {"action": name, "command": command, "cwd": cwd}

    if name == "emit_lever_map":
        levers = _parse_lever_map(action.get("levers", []))
        return {"action": name, "levers": levers}

    if name == "submit_hypothesis":
        hypothesis = action.get("hypothesis")
        card = _parse_hypothesis_card(hypothesis) if isinstance(hypothesis, dict) else None
        if card is None:
            raise GeneratorError(
                "submit_hypothesis.hypothesis must have at least one of "
                "region/mechanism/intervention_family"
            )
        return {"action": name, "hypothesis": card}

    raise GeneratorError(f"unknown generator action: {name}")


# --- Generator runtime protocol --------------------------------------------

_GEN_PROTOCOL = """Runtime contract (immutable):
Return exactly one JSON object per response, with no prose outside it.

Research tools (use freely to survey the subject matter):
- {"action":"run_research_command","command":"...","cwd":"work|scratch"}
  Run a bounded shell command (ls, grep, head, wc, git log, etc.) in your
  writable lab (/work) or scratch (/scratch). /work is the accepted source
  tree's editable paths, materialized read-write: survey it, and write scratch
  code or build small probes when that helps you understand the structure.
  Git history (any prior experiment SHA) is readable via /repo; you cannot
  commit.

Lever map synthesis:
- {"action":"emit_lever_map",
  "levers":[{"part":"...","role":"...","structural_space":"..."}]}
  Each lever is free-form: part (what you're looking at), role (what it does),
  structural_space (where there is room to act). Size = whatever the survey
  revealed. You are responsible for whether the map reflects your survey.

Hypothesis emit (you are done when you submit):
- {"action":"submit_hypothesis",
  "hypothesis":{"generative_op":"G6","region":"...","mechanism":"...",
   "intervention_family":"...","why_plausible":"...","critical_unknown":"...",
   "facts_read":["factual observation 1","factual observation 2",...]}}
  Submit ONE hypothesis. facts_read is non-empty — each entry is a factual
  observation from your survey, and the hypothesis follows from these facts.
  You are responsible for whether the hypothesis is grounded in your map.

Runtime boundaries:
- /work is your writable lab (accepted source, read-write); /repo is the
  read-only Git repository; /scratch is temporary writable.
- You cannot commit (artifacts are the candidate's job). You cannot see
  history, experiments, findings, or prior outcomes.
""".strip()


# --- Repair messages for prerequisite couplings ----------------------------
# These explain *why*, not just "not allowed" -- so the agent understands the
# purpose and doesn't treat the coupling as a form-filling rule.

_REPAIR_MESSAGES = {
    "submit_before_map": (
        "You submitted a hypothesis before synthesizing your lever map. The "
        "map is what gives you grounded breadth — without it, you are likely "
        "fixating on whatever is most salient rather than surveying the whole "
        "landscape. Survey the subject matter, emit_lever_map, then submit "
        "hypotheses grounded in the map. Return exactly one JSON action object."
    ),
    "map_before_survey": (
        "You emitted a lever map before surveying the subject matter. The map "
        "is supposed to reflect what you actually found — a map without a "
        "survey is a form filled from imagination. Run run_research_command to "
        "survey first, then emit_lever_map from what you found. Return exactly "
        "one JSON action object."
    ),
    "wrong_generative_op": (
        "You submitted a hypothesis with generative_op {got}, but you were "
        "assigned the lenses {ops}. The generative_op records which lens "
        "produced the hypothesis, so reason through one of your assigned "
        "lenses to find the hypothesis, then label it with that lens. Return "
        "exactly one JSON action object with a generative_op from your "
        "assigned set."
    ),
}


def _repair_message(reason: str, **kwargs) -> str:
    msg = _REPAIR_MESSAGES.get(reason)
    if msg is None:
        return (
            f"Protocol correction required ({reason}). Return exactly one JSON "
            "action object matching the Runtime contract."
        )
    if kwargs:
        return msg.format(**kwargs)
    return msg


class GeneratorAgent(ResearchAgent):
    """Lever-space surveyor hypothesis generator."""

    _error_class = GeneratorError

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
            timeout_seconds=timeout_seconds,
            max_steps=max_steps,
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
        )

    def _parse_action(self, text: str) -> dict:
        return _parse_generator_action(text)

    def _build_system_prompt(
        self, prompt_dir: str | Path | None,
        assigned_ops: tuple[str, ...] | None,
    ) -> str:
        semantic = load_semantic('generator', prompt_dir).rstrip()
        if assigned_ops:
            valid = tuple(op for op in assigned_ops if op in _GENERATIVE_OPS)
            if valid:
                semantic = _replace_basis(semantic, valid)
        return f"{semantic}\n\n{_GEN_PROTOCOL}"

    def run(
        self,
        *,
        context: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        world_mount,
        prompt_dir: str | Path | None = None,
        assigned_ops: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        hypotheses_per_lane: int = 1,
        ideas_per_lens: int = 1,
    ) -> GenerationResult:
        """Produce hypothesis cards. ``context`` is the history-free
        generation context. The Generator surveys the subject matter,
        synthesizes a lever map, and diverges across it.

        When ``hypotheses_per_lane > 1``, the generator produces multiple
        cards across its assigned lenses (``ideas_per_lens`` per lens) in a
        single tool-loop session, sharing one survey + map across all emits.
        """
        system_prompt = self._build_system_prompt(prompt_dir, assigned_ops)
        messages = [{"role": "user", "content": context}]
        if hypotheses_per_lane > 1:
            ops_list = ", ".join(assigned_ops or [])
            batch_instruction = (
                f"\n\n## Batch generation\n\n"
                f"You are assigned {len(assigned_ops or [])} generative "
                f"lenses: {ops_list}, with {ideas_per_lens} idea(s) per "
                f"lens ({hypotheses_per_lane} total). The generative_op "
                f"field records which lens produced each hypothesis, so "
                f"reason through a lens to find the hypothesis, then label "
                f"it. Survey once (survey + map shared), then emit "
                f"{hypotheses_per_lane} hypotheses, submitting each via "
                f"submit_hypothesis as you go.\n\n"
                f"After each submit, you will be prompted to continue with "
                f"the next idea. Different lenses naturally point at "
                f"different mechanisms — use different levers from your map "
                f"for each idea. All your ideas will be pursued by your "
                f"partner, so be bold and broad."
            )
            messages[0] = {
                "role": "user",
                "content": context + batch_instruction,
            }
        return self._tool_loop(
            messages, system_prompt, source_path, repo_path, run_dir,
            world_mount, max_steps or self.max_steps,
            hypotheses_per_lane=hypotheses_per_lane,
            assigned_ops=assigned_ops,
        )

    def regenerate(
        self,
        *,
        context: str,
        feedback: dict,
        transcript: list[dict],
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        world_mount,
        prompt_dir: str | Path | None = None,
        assigned_ops: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        hypotheses_per_lane: int = 1,
        ideas_per_lens: int = 1,
    ) -> GenerationResult:
        """Regenerate one hypothesis after the Cognitive partner feeds back
        history the Generator couldn't see. The Generator re-surveys the
        subject matter, synthesizes a fresh lever map, and diverges.
        """
        system_prompt = self._build_system_prompt(prompt_dir, assigned_ops)
        feedback_text = (
            f"History feedback from your partner:\n"
            f"  observation: {feedback['observation']}\n"
            f"  relation_to_seed: {feedback['relation_to_seed']}\n"
            f"  evidence_refs: {list(feedback['evidence_refs'])}\n"
            f"  implication: {feedback['implication']}\n\n"
            f"This is factual history you cannot see. Re-survey the subject "
            f"matter, synthesize a fresh lever map, and diverge to drop a "
            f"new lead."
        )
        messages = [{"role": "user", "content": context}]
        messages.extend(transcript)
        messages.append({"role": "user", "content": feedback_text})
        return self._tool_loop(
            messages, system_prompt, source_path, repo_path, run_dir,
            world_mount, max_steps or self.max_steps,
            hypotheses_per_lane=hypotheses_per_lane,
            assigned_ops=assigned_ops,
        )

    def _tool_loop(
        self, messages: list, system_prompt: str,
        source_path: Path, repo_path: Path, run_dir: Path,
        world_mount, steps_budget: int,
        hypotheses_per_lane: int = 1,
        assigned_ops: tuple[str, ...] | None = None,
    ) -> GenerationResult:
        """Shared tool loop for run() and regenerate().

        The harness enforces two prerequisite couplings as a structural
        backstop:
          - emit_lever_map requires at least one run_research_command first
          - submit_hypothesis requires emit_lever_map first

        Everything else (survey depth, map quality, hypothesis grounding) is
        the LLM's responsibility. The harness does not verify fields or match
        references.

        When ``hypotheses_per_lane > 1``, collects multiple hypothesis cards
        before returning (batch generation -- one survey+map, multiple emits).
        """
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        state = WorkingState()
        cards: list[HypothesisCard] = []

        # Phase tracking for prerequisite couplings
        has_surveyed = False          # at least one run_research_command
        has_emitted_map = False       # emit_lever_map issued

        print(f"[generator] started max_steps={steps_budget} "
              f"hypotheses_per_lane={hypotheses_per_lane}", flush=True)
        with TemporaryDirectory(prefix="simpleloop-gen-") as scratch, \
                TemporaryDirectory(prefix="simpleloop-gen-session-") as session_root:
            home = Path(session_root) / "home"
            home.mkdir(mode=0o700)
            tools = ResearchTools(
                runtime=self.runtime,
                workspace=source_path,
                repo=repo_path,
                history_dir=None,  # no history mounts — history-blind by boundary, not prompt
                scratch=Path(scratch),
                world_mount=world_mount,
                home=home,
                memory_service=None,  # no history tools
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=0,
            )
            for _step_num in range(steps_budget):
                step = _step_num + 1
                print(f"[gen step {step}/{steps_budget}] thinking", flush=True)
                action, reply_text = self._step(
                    state, messages, system_prompt, deadline, usages, step,
                    source_root=source_path, steps_budget=steps_budget,
                )
                name = action["action"]
                state.action_log.append({"action": name, "step": step})

                if name == "emit_lever_map":
                    # Prerequisite: must have surveyed first.
                    if not has_surveyed:
                        state.protocol_repairs += 1
                        print(
                            f"[gen step {step}/{steps_budget}] "
                            f"gate: map before survey -- rejected",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": _repair_message(
                                "map_before_survey")},
                        ])
                        continue
                    _bump(state, name)
                    has_emitted_map = True
                    print(
                        f"[gen step {step}/{steps_budget}] "
                        f"lever map: {len(action['levers'])} levers",
                        flush=True,
                    )
                    messages.extend([
                        {"role": "assistant", "content": reply_text},
                        {"role": "user", "content": (
                            "Lever map recorded. Now diverge across the map: "
                            "for each lever a generative lens makes visible, "
                            "submit_hypothesis grounded in that lever. Be "
                            "bold and broad. Return exactly one JSON action "
                            "object."
                        )},
                    ])
                    continue

                if name == "submit_hypothesis":
                    # Prerequisite: must have emitted a lever map.
                    if not has_emitted_map:
                        state.protocol_repairs += 1
                        print(
                            f"[gen step {step}/{steps_budget}] "
                            f"gate: submit before map -- rejected",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": _repair_message(
                                "submit_before_map")},
                        ])
                        continue
                    # Prerequisite: generative_op must be in assigned_ops.
                    card = action["hypothesis"]
                    if (assigned_ops
                            and card.generative_op not in assigned_ops):
                        state.protocol_repairs += 1
                        print(
                            f"[gen step {step}/{steps_budget}] "
                            f"gate: generative_op {card.generative_op} "
                            f"not in assigned {list(assigned_ops)} -- rejected",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": _repair_message(
                                "wrong_generative_op",
                                ops=assigned_ops,
                                got=card.generative_op)},
                        ])
                        continue
                    _bump(state, name)
                    cards.append(card)
                    print(
                        f"[generator] submit_hypothesis "
                        f"{card.signature()} steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s "
                        f"({len(cards)}/{hypotheses_per_lane})",
                        flush=True,
                    )
                    if len(cards) >= hypotheses_per_lane:
                        if assigned_ops:
                            used_ops = {c.generative_op for c in cards}
                            unused = set(assigned_ops) - used_ops
                            if unused:
                                print(
                                    f"[generator] batch done; "
                                    f"unused lenses: {sorted(unused)}",
                                    flush=True,
                                )
                        return GenerationResult(cards=cards, usage=usages)
                    # Prompt for the next idea in the batch.
                    remaining = hypotheses_per_lane - len(cards)
                    messages.extend([
                        {"role": "assistant", "content": reply_text},
                        {"role": "user", "content": (
                            f"Hypothesis {len(cards)}/"
                            f"{hypotheses_per_lane} recorded. Continue "
                            f"with the next idea ({remaining} remaining). "
                            f"You have multiple levers in your map — each "
                            f"idea is an opportunity to explore a different "
                            f"one. Return exactly one JSON action object."
                        )},
                    ])
                    continue

                # tool call (run_research_command) -- survey
                observation = tools.execute(action, deadline=deadline)
                _bump(state, "tool")
                _register_evidence(state, action, observation)
                state.last_tool_fingerprint = (
                    f"{action['action']}:{action.get('cwd')}:{action.get('command')}"
                )
                if observation.get("ok") and name == "run_research_command":
                    _bump(state, "source_read")
                    state.located = True
                    has_surveyed = True
                print(
                    f"[gen step {step}/{steps_budget}] "
                    f"{_result_summary(action, observation)}",
                    flush=True,
                )
                envelope = {
                    "state": _render_state_header(state, None),
                    "tool_result": observation,
                }
                messages.extend([
                    {"role": "assistant", "content": reply_text},
                    {"role": "user", "content": json.dumps(
                        envelope, ensure_ascii=False,
                    )},
                ])

        # Budget exhausted. In batch mode, return whatever cards were
        # collected (partial batch is better than nothing -- the cognitive
        # side can still audit fewer seeds). In single-card mode, raise.
        if cards:
            print(
                f"[generator] budget exhausted with {len(cards)}/"
                f"{hypotheses_per_lane} hypotheses -- returning partial batch",
                flush=True,
            )
            return GenerationResult(cards=cards, usage=usages)
        raise GeneratorError(
            f"generator budget exhausted ({steps_budget} steps) "
            "without submit_hypothesis"
        )
