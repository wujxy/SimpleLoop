"""Probe: shallow evidence check confirming a hypothesis's mechanism exists.

The probe layer sits between the Generator (wide, evidence-free) and the branch
researcher (deep, per-hypothesis). It runs 1-2 read-only shell commands per
card to confirm the mechanism the card points at is actually present in the
code — e.g. if the card says "repeated getter calls in EVLikelihood.cc", the
probe greps for getter calls in that file. Cards that fail the probe are
dropped before any deep budget is spent on them.

This is the "filter uses evidence, not prior" layer from PLAN.md. It is NOT a
judgment of whether the mechanism is the bottleneck — only that it exists.
"""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass

from .hypothesis import HypothesisCard, ProbeResult


@dataclass
class _ProbeCommand:
    command: str
    cwd: str = "source"


def _probe_commands(card: HypothesisCard) -> list[_ProbeCommand]:
    """Derive 1-2 read-only shell commands from the card's structural fields.

    Strategy:
    - If region looks like a file path → grep for a mechanism keyword in it.
    - Else → grep across the source tree for the mechanism keyword.
    - A second broader grep (just the intervention_family keyword) runs if the
      first returns nothing, to catch cases where the mechanism phrasing
      differs but the intervention is still present.
    """
    region = card.region.strip()
    mechanism_kw = _keyword(card.mechanism) or _keyword(card.intervention_family)
    interv_kw = _keyword(card.intervention_family)

    cmds: list[_ProbeCommand] = []
    if _looks_like_path(region):
        if mechanism_kw:
            cmds.append(_ProbeCommand(
                f"grep -rn --include='*.cc' --include='*.h' -l -- {shlex.quote(mechanism_kw)} {shlex.quote(region)} 2>/dev/null | head -5"
            ))
        else:
            # No keyword to grep — just check the file exists.
            cmds.append(_ProbeCommand(f"test -f {shlex.quote(region)} && echo FOUND"))
    else:
        # Region is a tag, not a path — search the tree.
        if mechanism_kw:
            cmds.append(_ProbeCommand(
                f"grep -rn --include='*.cc' --include='*.h' -l -- {shlex.quote(mechanism_kw)} . 2>/dev/null | head -5"
            ))
    # Second probe: intervention-family keyword (broader, fallback).
    if interv_kw and interv_kw != mechanism_kw:
        scope = shlex.quote(region) if _looks_like_path(region) else "."
        cmds.append(_ProbeCommand(
            f"grep -rn --include='*.cc' --include='*.h' -l -- {shlex.quote(interv_kw)} {scope} 2>/dev/null | head -5"
        ))
    if not cmds:
        # No keywords at all — emit a trivial "ls the region" probe so we don't
        # silently pass everything.
        if _looks_like_path(region):
            cmds.append(_ProbeCommand(f"ls -d {shlex.quote(region)} 2>/dev/null"))
        else:
            cmds.append(_ProbeCommand("find . -name '*.cc' -o -name '*.h' 2>/dev/null | head -1"))
    return cmds[:2]  # cap at 2


def _looks_like_path(text: str) -> bool:
    return "/" in text or text.endswith((".cc", ".h", ".cpp", ".hpp"))


def _keyword(text: str) -> str | None:
    """Extract a grep-friendly keyword from a free-form description.

    Picks the longest token (most selective), dropping common stopwords.
    Returns None if nothing usable.
    """
    if not text:
        return None
    stopwords = frozenset({
        "the", "a", "an", "in", "of", "for", "and", "or", "to", "is", "are",
        "with", "by", "on", "at", "from", "this", "that", "it", "as", "be",
        "not", "but", "if", "then", "else", "when", "which", "what", "how",
        "repeated", "per", "each", "every", "all", "some", "any", "no",
    })
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    candidates = [t for t in tokens if t.lower() not in stopwords and len(t) >= 2]
    if not candidates:
        return None
    # Prefer longer tokens (more selective); tie-break by first occurrence.
    return max(candidates, key=lambda t: (len(t), -candidates.index(t)))


def probe_hypothesis(
    card: HypothesisCard,
    command_runner,
    *,
    deadline: float,
    timeout_seconds: int = 30,
) -> ProbeResult:
    """Run the probe commands for one card. Returns confirmed if any command
    produced non-empty output (evidence the mechanism exists in the code).

    ``command_runner`` is a ResearchCommandRunner (or compatible) with a
    ``run(command, cwd=..., timeout_seconds=...)`` method returning
    ``{"ok": bool, "returncode": int, "output": str, ...}``.
    """
    cmds = _probe_commands(card)
    for pc in cmds:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ProbeResult(confirmed=False, note="probe deadline exceeded")
        obs = command_runner.run(
            pc.command, cwd=pc.cwd,
            timeout_seconds=min(timeout_seconds, remaining),
        )
        if not obs.get("ok"):
            continue
        output = obs.get("output", "")
        if output and output.strip():
            # Extract the first matching file path as the evidence ref.
            first_line = output.strip().splitlines()[0]
            ref = f"source:{first_line.rstrip(':')}" if first_line else None
            return ProbeResult(
                confirmed=True, evidence_ref=ref,
                note=f"probe hit: {first_line[:80]}",
            )
    return ProbeResult(confirmed=False, note="no probe command produced output")


def probe_batch(
    cards: list[HypothesisCard],
    command_runner,
    *,
    deadline: float,
    timeout_seconds: int = 30,
) -> list[tuple[HypothesisCard, ProbeResult]]:
    """Probe a batch of cards sequentially. Returns (card, result) pairs.

    Cards are probed in order; the deadline is shared across the batch so a
    slow first probe doesn't starve later ones (it just shortens them).
    """
    out = []
    for card in cards:
        result = probe_hypothesis(
            card, command_runner,
            deadline=deadline, timeout_seconds=timeout_seconds,
        )
        out.append((card, result))
        if deadline - time.monotonic() <= 0:
            break
    return out
