"""Generator: produces many lightweight hypothesis cards in one model call.

This is the wide, shallow, evidence-free first stage of the branch-then-deepen
architecture (PLAN.md). It uses G1-G9 as entry-point angles and outputs a JSON
array of HypothesisCard. A branch researcher later investigates each card
deeply; cards that point at non-existent mechanisms are abandoned by the
branch (producing a finding for Explore).

The generator never reads code and never forms a proposal. Its only inputs are
the startup-pack context (objective/gates/editable/frontier) and the generation
boundary (Explore's negative feedback: which (region, mechanism) families are
exhausted). A fraction of slots are frame-free — they ignore the boundary to
keep injecting fresh directions even when Explore has narrowed the space.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .model import ChatModel, ModelError, ModelReply
from .hypothesis import HypothesisCard
from ..explore.models import ExploreReport
from ..explore.families import normalize_region
from ..explore.render import render_generation_boundary
from ..prompts import load_semantic


# How many G operations to spread across (the prompt defines G1-G9).
_GENERATIVE_OPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9")

# Explore families with consecutive_no_improve at or above this threshold are
# "exhausted" — the generator is told to avoid them in guided slots. Higher
# than the stall threshold (4) used for the old submit-gate, because as a
# steering signal a false positive is costlier (it cuts a whole region).
_EXHAUSTED_NO_IMPROVE = 5


def _g_definition(semantic: str, op: str) -> str:
    """Extract one G's definition paragraph from the generator prompt text.

    The prompt stores each generative operation as a paragraph beginning with
    ``Gn — <title>: <body>`` (possibly wrapped across lines), separated by blank
    lines. Returns the single paragraph for ``op`` (e.g. "G6"), or "" if the
    shape is not recognized — the caller falls back to the full basis.
    """
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
    """Swap the full G1-G9 basis in the prompt for a scheduler-selected subset.

    The prompt has a ``## The Generative Basis ...`` section whose body is the
    G1-G9 paragraphs. This replaces that body with the definition paragraphs of
    ``ops`` (in the given order), leaving the section header and everything
    else (rules, JSON schema) untouched. If the section shape isn't recognized,
    returns ``semantic`` unchanged so a prompt edit never breaks generation.
    """
    lines = semantic.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("## The Generative Basis"):
            header_idx = i
            break
    if header_idx is None:
        return semantic
    # The basis body runs from the line after the header up to (but not
    # including) the next blank-line-then-non-G paragraph — i.e. the closing
    # "You are not required to use every G." paragraph.
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


@dataclass(frozen=True)
class GenerationResult:
    cards: list[HypothesisCard]
    usage: object = None


class GeneratorAgent:
    """One-shot hypothesis generator. Not a state machine — a single model call."""

    def __init__(
        self,
        *,
        model: ChatModel,
        timeout_seconds: int,
        usage_observer=None,
    ):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.usage_observer = usage_observer

    def run(
        self,
        *,
        n: int,
        frame_free_ratio: float,
        context: str,
        explore: ExploreReport | None = None,
        prompt_dir: str | None = None,
        assigned_ops: tuple[str, ...] | None = None,
    ) -> GenerationResult:
        """Produce n hypothesis cards. ``context`` is the startup-pack text
        (objective/gates/editable/frontier). ``explore`` supplies the negative
        feedback boundary; None means no boundary (first round).

        ``assigned_ops`` is a *soft* steering signal from the orchestrator's
        generative-op scheduler: a subset of G1-G9 (e.g. ("G2","G6","G9",...))
        the model may choose from this call. The full G1-G9 basis in the prompt
        is replaced with just these entries (with their definitions), so the
        model's choice is constrained to the subset. The model still
        self-reports ``generative_op`` in the JSON — the delivery contract is
        unchanged; this is guidance, not an override.
        """
        boundary = render_generation_boundary(explore)
        n_free = max(1, int(n * frame_free_ratio)) if n > 1 else 0
        n_guided = n - n_free
        semantic = load_semantic('generator', prompt_dir).rstrip()
        if assigned_ops:
            valid = tuple(op for op in assigned_ops if op in _GENERATIVE_OPS)
            if valid:
                semantic = _replace_basis(semantic, valid)
        system = (
            f"{semantic}\n\n"
            f"Produce exactly {n} hypotheses: {n_guided} guided slot(s) "
            f"(respect the exhausted-region list) and {n_free} free slot(s) "
            f"(ignore it — generate from pure imagination). Mark each with "
            f"\"slot\":\"guided\" or \"slot\":\"free\".\n\n"
            f"{boundary}\n\n"
            f"Return exactly one JSON object: "
            f'{{"hypotheses":[{{"generative_op":"G6",'
            f'"region":"...","mechanism":"...","intervention_family":"...",'
            f'"why_plausible":"...","critical_unknown":"...","slot":"guided"}},'
            f"...]}}. No prose outside the JSON."
        )
        messages = [{"role": "user", "content": context}]
        deadline = time.monotonic() + self.timeout_seconds
        reply = self.model.complete(
            system=system, messages=messages,
            timeout_seconds=min(self.timeout_seconds, deadline - time.monotonic()),
        )
        if self.usage_observer is not None and reply.usage is not None:
            self.usage_observer(reply.usage)
        cards = _parse_hypotheses(reply.text, expected=n)
        return GenerationResult(cards=cards, usage=reply.usage)


def _parse_hypotheses(text: str, *, expected: int) -> list[HypothesisCard]:
    """Parse the model's JSON response into cards. Tolerates over/under-production
    but warns via count mismatch (the caller decides whether to retry)."""
    try:
        obj = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ModelError(f"generator response is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("hypotheses"), list):
        raise ModelError("generator response must be {\"hypotheses\":[...]}")
    raw = obj["hypotheses"]
    cards: list[HypothesisCard] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        op = str(item.get("generative_op", "G6")).strip()
        if op not in _GENERATIVE_OPS:
            op = "G6"
        region = str(item.get("region", "")).strip()
        mechanism = str(item.get("mechanism", "")).strip()
        intervention = str(item.get("intervention_family", "")).strip()
        why = str(item.get("why_plausible", "")).strip()
        unknown = str(item.get("critical_unknown", "")).strip()
        slot = str(item.get("slot", "guided")).strip()
        if slot not in ("guided", "free"):
            slot = "guided"
        # Skip cards that are empty in all three structural fields — they
        # carry no direction and would dedup to the unknown niche.
        if not region and not mechanism and not intervention:
            continue
        cards.append(HypothesisCard(
            generative_op=op, region=region, mechanism=mechanism,
            intervention_family=intervention, why_plausible=why,
            critical_unknown=unknown, slot=slot,
        ))
    if not cards:
        raise ModelError("generator produced no usable hypotheses")
    return cards
