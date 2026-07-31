"""Invoke the development-time Meta Optimizer on the host."""
from __future__ import annotations

import os
from pathlib import Path

from ..prompts import load_semantic
from ..roles.agent import Agent


class LocalRuntime:
    """Small runtime adapter that lets the existing Agent run on the host."""

    def exec_argv(self, payload: list[str], *, cwd: Path) -> list[str]:
        return payload

    def subprocess_env(self, extra: dict[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        env.update(extra)
        return env


class MetaOptimizer:
    def __init__(
        self,
        command: str,
        timeout_seconds: int,
        max_output_tokens: int,
        *,
        agent_factory=Agent,
    ):
        self.agent = agent_factory(
            runtime=LocalRuntime(), command=command,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            allowed_tools="Read,Edit,Write,Bash",
        )

    def run(
        self,
        *,
        run_dir: str | Path,
        prompt_dir: str | Path,
        history_dir: str | Path,
        source_dir: str | Path,
        goal: str,
        gate_block: str,
    ) -> None:
        run_dir = Path(run_dir).resolve()
        prompt_dir = Path(prompt_dir).resolve()
        history_dir = Path(history_dir).resolve()
        source_dir = Path(source_dir).resolve()
        semantic = load_semantic("meta_optimizer", prompt_dir)
        prompt = f"""{semantic}

Task Goal:
{goal}

User Gates:
{gate_block}

Read access:
- current run history: {run_dir}
- prompt history: {history_dir}
- active prompts: {prompt_dir}
- relevant SimpleLoop source: {source_dir}

Write access:
- {prompt_dir / 'proposer.md'}
- {prompt_dir / 'executor.md'}
- {prompt_dir / 'judger.md'}
- {prompt_dir / 'meta_optimizer.md'} outside META_IDENTITY_CORE
- {prompt_dir / 'optimizer_report.yaml'}

Fixed artifacts:
- META_IDENTITY_CORE
- machine protocols and schemas
- SimpleLoop harness source
- task Goal and user Gates
- run history and measured results

Edit the active prompt files directly. Write optimizer_report.yaml with exactly:
- status: changed or no_change
- diagnosis: non-empty text
- evidence: a list of references selected during the investigation
- intent: text, empty for no_change
"""
        self.agent.run_text(prompt, cwd=prompt_dir, label="meta optimizer")
