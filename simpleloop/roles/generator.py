"""Generator: a code-reading agent that produces one hypothesis card per call.

The Generator is the free explorer in a partner lane. It can read the source
tree (via ``run_research_command`` on the parent-sha checkout) to find real
files and functions, but it sees NO history — no dashboard, no frontier, no
exhausted-region list, no prior outcomes. It reasons from the objective, the
source structure it reads, and a 5-of-9 generative-op subset to produce one
unverified seed (``submit_hypothesis``).

The bound Cognitive element is its partner — it audits the seed against
history the Generator can't see, enriches it into a proposal, or feeds history
back for regeneration.
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


def _parse_hypothesis_card(item: dict) -> HypothesisCard | None:
    """Parse one hypothesis dict from submit_hypothesis. Returns None if the
    card is empty in all three structural fields.

    ``facts_read`` is required: non-empty list of non-empty strings. Each
    string is a factual observation from reading the source (NOT a code
    snippet). Missing/empty facts_read → GeneratorError (Gate 2).
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
    # Gate 2: facts_read must be a non-empty list of non-empty strings.
    raw_facts = item.get("facts_read")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise GeneratorError(
            "submit_hypothesis.hypothesis.facts_read must be a non-empty "
            "list of factual observations from the source"
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


# --- Generator action parsing ----------------------------------------------

def _parse_generator_action(text: str) -> dict:
    """Parse one action from the Generator's model reply.

    Terminal: ``submit_hypothesis``.
    Non-terminal: ``run_research_command`` (read source).
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
        cwd = action.get("cwd", "source")
        if not isinstance(command, str) or not command.strip():
            raise GeneratorError("research command must be non-empty")
        if cwd not in {"source", "scratch"}:
            raise GeneratorError("research cwd must be source or scratch")
        return {"action": name, "command": command, "cwd": cwd}

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

Research tools (use freely to explore the source tree):
- {"action":"run_research_command","command":"...","cwd":"source|scratch"}
  Inspect the accepted source with a bounded shell command (ls, grep, head, wc,
  git log, etc.). Source is read-only; scratch is writable. Use this to find
  real files and functions before submitting your hypothesis.

Control action (you are done when you submit):
- {"action":"submit_hypothesis",
  "hypothesis":{"generative_op":"G6","region":"...","mechanism":"...",
   "intervention_family":"...","why_plausible":"...","critical_unknown":"...",
   "facts_read":["factual observation 1","factual observation 2",...]}}
  Submit ONE hypothesis. You MUST have run at least one run_research_command
  first, and facts_read MUST be non-empty. Each entry in facts_read is a
  factual observation you made by reading the source (e.g. "the likelihood
  loop iterates per-PMT in OMILRECV2.cc") — NOT a code snippet or line
  content. The hypothesis must follow from these facts. region must be a real
  file path you verified. The hypothesis is a lead (mechanism + intervention
  family), not an implementation plan.

Runtime boundaries:
- /source is the accepted revision (read-only), /scratch is temporary writable.
- You cannot see history, experiments, findings, or prior outcomes.
""".strip()


class GeneratorAgent(ResearchAgent):
    """Code-reading hypothesis generator. Reuses the shared tool loop."""

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
            timeout_seconds=timeout_seconds, max_steps=max_steps,
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
        prompt_dir: str | Path | None = None,
        assigned_ops: tuple[str, ...] | None = None,
        max_steps: int | None = None,
    ) -> GenerationResult:
        """Produce one hypothesis card. ``context`` is the history-free
        generation context. The Generator can read the source tree via
        ``run_research_command`` to find real files before submitting.
        """
        system_prompt = self._build_system_prompt(prompt_dir, assigned_ops)
        messages = [{"role": "user", "content": context}]
        return self._tool_loop(
            messages, system_prompt, source_path, repo_path, run_dir,
            max_steps or self.max_steps,
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
        prompt_dir: str | Path | None = None,
        assigned_ops: tuple[str, ...] | None = None,
        max_steps: int | None = None,
    ) -> GenerationResult:
        """Regenerate one hypothesis after the Cognitive partner feeds back
        history the Generator couldn't see. The Generator can read the source
        again to find a real region for the new hypothesis.
        """
        system_prompt = self._build_system_prompt(prompt_dir, assigned_ops)
        feedback_text = (
            f"History feedback from your partner:\n"
            f"  observation: {feedback['observation']}\n"
            f"  relation_to_seed: {feedback['relation_to_seed']}\n"
            f"  evidence_refs: {list(feedback['evidence_refs'])}\n"
            f"  implication: {feedback['implication']}\n\n"
            f"This is factual history you cannot see. Read the source to find "
            f"a real region for your new hypothesis, then submit_hypothesis."
        )
        messages = [{"role": "user", "content": context}]
        messages.extend(transcript)
        messages.append({"role": "user", "content": feedback_text})
        return self._tool_loop(
            messages, system_prompt, source_path, repo_path, run_dir,
            max_steps or self.max_steps,
        )

    def _tool_loop(
        self, messages: list, system_prompt: str,
        source_path: Path, repo_path: Path, run_dir: Path,
        steps_budget: int,
    ) -> GenerationResult:
        """Shared tool loop for run() and regenerate()."""
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        state = WorkingState()

        print(f"[generator] started max_steps={steps_budget}", flush=True)
        with TemporaryDirectory(prefix="simpleloop-gen-") as scratch:
            tools = ResearchTools(
                runtime=self.runtime,
                source=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                memory_service=None,  # no history tools
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=0,
            )
            has_read_source = False  # Gate 1: must read before submit
            for _step_num in range(steps_budget):
                step = _step_num + 1
                print(f"[gen step {step}/{steps_budget}] thinking", flush=True)
                action, reply_text = self._step(
                    state, messages, system_prompt, deadline, usages, step,
                    source_root=source_path, steps_budget=steps_budget,
                )
                name = action["action"]
                state.action_log.append({"action": name, "step": step})

                if name == "submit_hypothesis":
                    # Gate 1: reject submit if the generator never read the
                    # source. Feed a repair message and continue — do NOT
                    # raise (don't kill the lane), give it a chance to read
                    # then submit within the remaining budget.
                    if not has_read_source:
                        state.protocol_repairs += 1
                        print(
                            f"[gen step {step}/{steps_budget}] "
                            f"gate: submit before any source read — rejected",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": (
                                "You submitted a hypothesis without reading "
                                "the source first. This is not allowed. Run "
                                "run_research_command (e.g. ls, grep) to read "
                                "the source tree, THEN submit_hypothesis with "
                                "facts_read populated from what you observed. "
                                "Return exactly one JSON action object."
                            )},
                        ])
                        continue
                    _bump(state, name)
                    card = action["hypothesis"]
                    print(
                        f"[generator] submit_hypothesis "
                        f"{card.signature()} steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return GenerationResult(cards=[card], usage=usages)

                # tool call (run_research_command)
                observation = tools.execute(action, deadline=deadline)
                _bump(state, "tool")
                _register_evidence(state, action, observation)
                state.last_tool_fingerprint = (
                    f"{action['action']}:{action.get('cwd')}:{action.get('command')}"
                )
                if observation.get("ok") and name == "run_research_command":
                    _bump(state, "source_read")
                    state.located = True
                    has_read_source = True
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

        # Budget exhausted without submit_hypothesis — raise, the orchestrator
        # treats this lane as errored. The Generator must submit within budget.
        raise GeneratorError(
            f"generator budget exhausted ({steps_budget} steps) "
            "without submit_hypothesis"
        )
