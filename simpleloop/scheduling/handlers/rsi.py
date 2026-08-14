"""Self-edit Host adapter for the unified worker."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from ... import config as config_mod
from ...roles.agent import Agent
from ...world import (
    ApptainerSandbox,
    SandboxSpec,
    SourceWorkspace,
    WorldBuilder,
    executor_environment,
    executor_world_spec,
)


def handle_self_edit(
    payload: Mapping[str, object],
    observe_usage: Callable[[Mapping[str, object]], None],
) -> Mapping[str, object]:
    raw = dict(payload)
    try:
        agent = _build_agent(raw, observe_usage)
        output = agent.run_text(
            _prompt(raw.get("change")),
            cwd=Path(str(raw["worktree_path"])),
            label=f"self-exec r{int(raw['round_id'])}",
        )
        result = {"status": "EDITED", "output": output, "reason": ""}
    except (ValueError, RuntimeError) as exc:
        result = {"status": "EDITOR_FAILED", "output": "", "reason": str(exc)}
    return {"self_edit": result}


def _build_agent(raw: Mapping[str, object], observe_usage):
    run_dir = Path(str(raw["run_dir"]))
    cfg = config_mod.load_resolved(run_dir)
    role = (cfg.get("roles") or {}).get("executor") or {}
    workspace = SourceWorkspace(
        f"self-{int(raw['round_id'])}",
        Path(str(raw["worktree_path"])),
        "",
    )
    sandbox = ApptainerSandbox()
    spec = SandboxSpec(
        Path(cfg["runtime_image"]),
        executor_environment(
            base_url=role.get("base_url"),
            max_output_tokens=int(cfg.get("agent_max_output_tokens", 64000)),
        ),
        True,
    )
    sandbox.preflight(spec)
    world = WorldBuilder(sandbox).build(
        workspace, spec, executor_world_spec(("proposer",)),
    )
    return Agent(
        world=world,
        command="claude",
        timeout_seconds=int(cfg.get("agent_timeout_seconds", 3600)),
        allowed_tools="Read,Edit,Write,Bash",
        model=role.get("model"),
        usage_observer=observe_usage,
    )


def _prompt(raw: object) -> str:
    change = raw if isinstance(raw, Mapping) else {}
    return f"""You are modifying the Scientist — the proposer/ package.

Target:
{change.get('target') or ''}

Intent:
{change.get('intent') or ''}

Change to make:
{change.get('instruction') or ''}

Edit the proposer/ source in this workspace. Do not run Git or touch .git.
The Host inspects and commits your filesystem changes after you stop.
"""
