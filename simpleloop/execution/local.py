"""LocalBackend: candidates run in frontend threads, exactly the historical
behavior. The dispatch itself stays in loop._run_candidates (its serial and
ThreadPool paths); this adapter only exists so loop.py can treat both
backends uniformly. Local runs never persist in-flight state, so the journal
is accepted and ignored. The deferred loop import avoids a module cycle
(loop imports execution for the backend factory)."""
from __future__ import annotations

from .base import ExecutionBackend


class LocalBackend(ExecutionBackend):
    def __init__(self, ctx):
        self.ctx = ctx

    def run_candidates(self, *, proposals: list[dict], round_id: int,
                       parent_sha: str, prior_metrics: dict,
                       baseline_metrics: dict, journal=None) -> list[dict]:
        from .. import loop as loop_mod
        return loop_mod._run_candidates(
            self.ctx, proposals, round_id, parent_sha, prior_metrics,
            baseline_metrics)

    def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
        """Run the baseline eval locally on the baseline worktree.

        Returns (eval_block, metrics). Raises BaselineAcceptanceError on failure.
        """
        from ..harness import evals
        from ..loop import BaselineAcceptanceError

        cfg = self.ctx.cfg
        runtime = self.ctx.runtime
        workspace = self.ctx.workspace
        from ..loop import stamp
        import subprocess

        print(f"[{stamp()}] running baseline eval (on {baseline_sha[:10]}) for the judger's "
              f"vs-baseline axis...", flush=True)
        wt = None
        try:
            wt = workspace.add_worktree("baseline", baseline_sha)
            bind_paths = [
                *(str(path) for path in runtime.binds
                  if path != runtime.run_dir),
                str(runtime.run_dir),
            ]
            context = (
                f"image: {runtime.image}\n"
                f"binds: {', '.join(bind_paths)}\n"
                f"cwd: {wt}"
            )
            try:
                result = evals.run_eval(
                    cfg["eval_commands"],
                    cwd=wt,
                    runtime=runtime,
                    metrics_schema=cfg.get("metrics"),
                    timeout_seconds=cfg.get("eval_timeout_seconds", 600),
                    output_cap=cfg.get("eval_output_cap_chars", 16000),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BaselineAcceptanceError(
                    "baseline evaluation failed to run: "
                    f"{exc}\n{context}"
                ) from exc
            try:
                self._require_baseline_acceptance(result, cfg.get("metrics"))
            except BaselineAcceptanceError as exc:
                raise BaselineAcceptanceError(
                    f"{exc}\n{context}"
                ) from exc
            print(f"[{stamp()}] baseline eval done.", flush=True)
            return result.text, result.metrics
        finally:
            if wt is not None:
                workspace.remove_worktree("baseline")

    def _require_baseline_acceptance(
        self, result: evals.EvalResult,
        metrics_schema: dict,
    ) -> None:
        """Reject an unusable baseline before any optimization agent is called."""
        import math
        from ..loop import BaselineAcceptanceError

        failed_codes = [code for code in result.returncodes if code != 0]
        if failed_codes:
            raise BaselineAcceptanceError(
                "baseline evaluation command failed with exit "
                f"{failed_codes[0]}:\n{result.text[:8000]}"
            )

        objective = metrics_schema["objective"]["key"]
        value = result.metrics.get(objective)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise BaselineAcceptanceError(
                f"baseline objective {objective} is missing or not finite:\n"
                f"{result.text[:8000]}"
            )

        failed_gates = [
            gate["key"]
            for gate in metrics_schema.get("gates", [])
            if result.metrics.get(gate["key"]) is not True
        ]
        if failed_gates:
            raise BaselineAcceptanceError(
                "baseline gate(s) did not pass: "
                f"{', '.join(failed_gates)}:\n{result.text[:8000]}"
            )
