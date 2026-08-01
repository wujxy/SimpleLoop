"""Bounded scientific Proposer agent with read-only research tools."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from .model import ChatModel
from .research_tools import Insight, ResearchTools, render_insights
from ..container.runtime import ApptainerRuntime
from ..harness import views
from ..prompts import load_semantic


class ProposerError(RuntimeError):
    """The Proposer violated its action or budget contract."""


@dataclass(frozen=True)
class ProposerResult:
    proposals: list[str]
    insight: Insight | None
    usage: object = None


_RUNTIME_PROTOCOL = """
Runtime contract (immutable):
- Return exactly one JSON object per response, with no prose outside it.
- Available non-terminal actions:
  {"action":"run_research_command","command":"...","cwd":"source|scratch"}
  {"action":"search_history","query":"..."}
  {"action":"inspect_episode","ref":"r<round>c<candidate>"}
  {"action":"write_insight","text":"1..500 chars","refs":["r0c0"]}
- Terminal action:
  {"action":"submit_proposals","proposals":["executable instruction"]}
- /source is the accepted revision, /repo is its read-only Git repository,
  /history.jsonl and /rounds are persisted run evidence when present, and
  /scratch is temporary writable space.
- You cannot call the Executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
- Choose research actions and their order adaptively. No action is mandatory.
""".strip()

_SUMMARY_PREVIEW_CHARS = 160


def _preview(value: str) -> str:
    text = " ".join(value.split())
    if len(text) <= _SUMMARY_PREVIEW_CHARS:
        return text
    return text[:_SUMMARY_PREVIEW_CHARS - 3] + "..."


def _quoted_preview(value: str) -> str:
    return json.dumps(_preview(value), ensure_ascii=False)


def _action_summary(action: dict) -> str:
    name = action["action"]
    if name == "run_research_command":
        return (
            f"action={name} cwd={action['cwd']} "
            f"command={_quoted_preview(action['command'])}"
        )
    if name == "search_history":
        return f"action={name} query={_quoted_preview(action['query'])}"
    if name == "inspect_episode":
        return f"action={name} ref={_quoted_preview(action['ref'])}"
    if name == "write_insight":
        return f"action={name} refs={len(action['refs'])}"
    return f"action={name} count={len(action['proposals'])}"


def _result_summary(action: dict, observation: dict) -> str:
    parts = [f"result={'ok' if observation.get('ok') else 'error'}"]
    if action["action"] == "run_research_command":
        if "returncode" in observation:
            parts.append(f"exit_code={observation['returncode']}")
        output = observation.get("output")
        if isinstance(output, str):
            parts.append(f"output_chars={len(output)}")
        if "truncated" in observation:
            parts.append(
                f"truncated={str(bool(observation['truncated'])).lower()}"
            )
        if observation.get("timed_out"):
            parts.append("timed_out=true")
    elif action["action"] == "search_history":
        matches = observation.get("result")
        if isinstance(matches, list):
            parts.append(f"matches={len(matches)}")
    return " ".join(parts)


class ProposerAgent:
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
        self.model = model
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_cap_chars = command_output_cap_chars
        self.usage_observer = usage_observer

    def run(
        self,
        *,
        goal: str,
        editable: list[str],
        frozen: list[str],
        history: list[dict],
        insights: list[dict],
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        candidates_per_round: int,
        recent_rounds: int,
        gate_block: str,
        prompt_dir: Path | None,
    ) -> ProposerResult:
        system_prompt = (
            f"{load_semantic('proposer', prompt_dir).rstrip()}\n\n"
            f"{_RUNTIME_PROTOCOL}"
        )
        messages = [{
            "role": "user",
            "content": _initial_context(
                goal=goal,
                editable=editable,
                frozen=frozen,
                history=history,
                insights=insights,
                base_sha=base_sha,
                candidates_per_round=candidates_per_round,
                recent_rounds=recent_rounds,
                gate_block=gate_block,
            ),
        }]
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        print(f"[proposer] started max_steps={self.max_steps}", flush=True)
        with TemporaryDirectory(prefix="simpleloop-research-") as scratch:
            tools = ResearchTools(
                runtime=self.runtime,
                source=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                history=history,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
            )
            for _step in range(self.max_steps):
                step = _step + 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProposerError("proposer deadline exceeded")
                print(
                    f"[proposer step {step}/{self.max_steps}] thinking",
                    flush=True,
                )
                reply = self.model.complete(
                    system=system_prompt,
                    messages=messages,
                    timeout_seconds=remaining,
                )
                usages.append(reply.usage)
                if self.usage_observer is not None and reply.usage is not None:
                    self.usage_observer(reply.usage)
                action = _parse_action(
                    reply.text,
                    candidates_per_round=candidates_per_round,
                )
                print(
                    f"[proposer step {step}/{self.max_steps}] "
                    f"{_action_summary(action)}",
                    flush=True,
                )
                if action["action"] == "submit_proposals":
                    print(
                        f"[proposer] finished steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return ProposerResult(
                        proposals=action["proposals"],
                        insight=tools.pending_insight,
                        usage=usages,
                    )
                observation = tools.execute(action, deadline=deadline)
                print(
                    f"[proposer step {step}/{self.max_steps}] "
                    f"{_result_summary(action, observation)}",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": json.dumps(
                        {"tool_result": observation}, ensure_ascii=False,
                    )},
                ])
        raise ProposerError("proposer exceeded researcher.max_steps")


def _initial_context(
    *,
    goal: str,
    editable: list[str],
    frozen: list[str],
    history: list[dict],
    insights: list[dict],
    base_sha: str,
    candidates_per_round: int,
    recent_rounds: int,
    gate_block: str,
) -> str:
    recent = views.for_proposer(history, recent_rounds=recent_rounds)
    facts = json.dumps(recent, ensure_ascii=False, indent=2) if recent else "[]"
    return f"""Research objective:
{goal}

Harness Gates:
{gate_block or "(declared in factual records)"}

Current accepted revision: {base_sha}
Editable paths: {json.dumps(editable, ensure_ascii=False)}
Frozen paths: {json.dumps(frozen, ensure_ascii=False)}

Recent factual outcomes:
{facts}

Proposer Insights:
{render_insights(insights)}

Insights are fallible hypotheses and factual-history indexes, not Harness facts.
Submit exactly {candidates_per_round} nonblank executable proposal(s).
Every candidate begins from the accepted revision above.
"""


def _parse_action(text: str, candidates_per_round: int) -> dict:
    try:
        action = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProposerError("proposer response must be one JSON object") from exc
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise ProposerError("proposer action must be a JSON object with action")
    name = action["action"]
    if name == "run_research_command":
        _require_keys(action, {"action", "command"}, {"cwd"})
        command = action["command"]
        cwd = action.get("cwd", "source")
        if not isinstance(command, str) or not command.strip():
            raise ProposerError("research command must be non-empty")
        if cwd not in {"source", "scratch"}:
            raise ProposerError("research cwd must be source or scratch")
        return {"action": name, "command": command, "cwd": cwd}
    if name == "search_history":
        _require_keys(action, {"action", "query"})
        query = action["query"]
        if not isinstance(query, str) or not query.strip():
            raise ProposerError("history query must be non-empty")
        return {"action": name, "query": query.strip()}
    if name == "inspect_episode":
        _require_keys(action, {"action", "ref"})
        ref = action["ref"]
        if not isinstance(ref, str) or not ref.strip():
            raise ProposerError("episode ref must be non-empty")
        return {"action": name, "ref": ref.strip()}
    if name == "write_insight":
        _require_keys(action, {"action", "text", "refs"})
        insight_text = action["text"]
        refs = action["refs"]
        if not isinstance(insight_text, str) or not insight_text.strip():
            raise ProposerError("insight text must be non-empty")
        if (not isinstance(refs, list) or not refs
                or not all(isinstance(ref, str) for ref in refs)):
            raise ProposerError("insight refs must be a non-empty string list")
        return {
            "action": name,
            "text": insight_text.strip(),
            "refs": refs,
        }
    if name == "submit_proposals":
        _require_keys(action, {"action", "proposals"})
        proposals = action["proposals"]
        if (not isinstance(proposals, list)
                or len(proposals) != candidates_per_round):
            raise ProposerError(
                f"expected exactly {candidates_per_round} proposals"
            )
        normalized = []
        for proposal in proposals:
            if not isinstance(proposal, str) or not proposal.strip():
                raise ProposerError("proposals must be nonblank strings")
            normalized.append(proposal.strip())
        return {"action": name, "proposals": normalized}
    raise ProposerError(f"unknown proposer action: {name}")


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
