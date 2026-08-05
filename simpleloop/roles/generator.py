"""Generator: produces many lightweight hypothesis cards in one model call.

This is the wide, shallow, evidence-free first stage of the branch-then-deepen
architecture (PLAN.md). It uses G1-G9 as entry-point angles and outputs a JSON
array of HypothesisCard. A probe layer later checks whether each mechanism
exists in the code; only confirmed cards enter a branch researcher.

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
    ) -> GenerationResult:
        """Produce n hypothesis cards. ``context`` is the startup-pack text
        (objective/gates/editable/frontier). ``explore`` supplies the negative
        feedback boundary; None means no boundary (first round)."""
        boundary = render_generation_boundary(explore)
        n_free = max(1, int(n * frame_free_ratio)) if n > 1 else 0
        n_guided = n - n_free
        system = (
            f"{load_semantic('generator', prompt_dir).rstrip()}\n\n"
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
