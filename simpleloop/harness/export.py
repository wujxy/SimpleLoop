"""Export a run's winning commit out of the per-run clone.

The loop leaves its result as a SHA inside run_dir/repo — a clone the user
should not have to dig through. `simpleloop export` materializes it:

  run_dir/export/<what>.diff    squashed `git diff baseline..target` (review)
  run_dir/export/<what>.bundle  full commit chain; `git fetch <bundle>` imports
                                it losslessly into any clone holding baseline
  run_dir/export/EXPORT.md      metric summary + ready-to-run import commands

`--to-branch NAME` additionally pushes the target sha into the SOURCE repo as
a new branch (the per-run clone's origin IS the source repo, so a local push
completes the handover). An existing branch is refused — no force overwrite.

Reads only run_dir state (config.resolved.json, history.jsonl, telemetry.json)
— pure git on the host, no container needed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config as config_mod
from ..reporting import telemetry as telemetry_mod
from . import memory as memory_mod
from .store import best_candidate


class ExportError(RuntimeError):
    """User-facing export failure."""


# Bundles need a real ref for the tip (a raw sha is not bundleable); the
# export ref is created/updated in the per-run repo, never in the source repo.
_EXPORT_REF_PREFIX = "refs/simpleloop/export"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise ExportError(
            f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _resolve_target(history: list[dict], schema: dict, what: str,
                    baseline_sha: str) -> tuple[str, str, dict]:
    """Return (target_sha, human description, target metrics) for best|head."""
    if what == "best":
        best = best_candidate(history, schema)
        if best is None:
            raise ExportError(
                "no eligible best candidate (all gates pass + numeric "
                "objective) — nothing to export")
        return (best["sha"], f"best candidate r{best['round']}c{best.get('candidate')}",
                best.get("metrics") or {})
    if what == "head":
        head_sha = history[-1].get("base_sha")
        if not head_sha or head_sha == baseline_sha:
            raise ExportError(
                "the accepted chain never advanced past the baseline — "
                "nothing to export")
        last_accepted = next(
            (r for r in reversed(history) if r.get("selected_sha")), {})
        return (head_sha,
                f"accepted chain head after round {history[-1].get('round')}",
                last_accepted.get("metrics") or {})
    raise ExportError(f"unknown export target {what!r}; use best|head")


def export_run(run_dir: str | Path, what: str = "best",
               to_branch: str | None = None) -> dict:
    """Export the run's best (or chain-head) commit. Returns an info dict."""
    run_path = Path(run_dir).expanduser().resolve()
    repo = run_path / "repo"
    if not (repo / ".git").exists():
        raise ExportError(f"no per-run repo at {repo} — is this a run_dir?")
    cfg = config_mod.load_resolved(run_path)
    history = memory_mod.read_history(run_path / "history.jsonl")
    if not history:
        raise ExportError(
            f"no rounds recorded in {run_path / 'history.jsonl'} — nothing to export")

    schema = cfg["metrics"]
    obj_key = schema["objective"]["key"]
    baseline_sha = _git(repo, "rev-parse", "--verify",
                        f"{cfg['baseline_ref']}^{{commit}}")
    target_sha, target_desc, target_metrics = _resolve_target(
        history, schema, what, baseline_sha)
    _git(repo, "rev-parse", "--verify", f"{target_sha}^{{commit}}")

    # Refuse an existing source-repo branch BEFORE producing anything.
    if to_branch:
        if _git(repo, "ls-remote", "--heads", "origin", to_branch):
            raise ExportError(
                f"branch {to_branch!r} already exists in the source repo; "
                "choose another name (no force overwrite)")

    export_dir = run_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    diff_path = export_dir / f"{what}.diff"
    diff_path.write_text(
        _git(repo, "diff", f"{baseline_sha}..{target_sha}") + "\n",
        encoding="utf-8")

    ref = f"{_EXPORT_REF_PREFIX}/{what}"
    _git(repo, "update-ref", ref, target_sha)
    bundle_path = export_dir / f"{what}.bundle"
    _git(repo, "bundle", "create", str(bundle_path),
         f"{baseline_sha}..{ref}")

    baseline_metrics = (telemetry_mod.load_plot_context(run_path)
                        .get("baseline_metrics") or {})
    md_path = export_dir / "EXPORT.md"
    md_path.write_text(_render_export_md(
        run_path=run_path, cfg=cfg, what=what, ref=ref,
        target_sha=target_sha, target_desc=target_desc,
        baseline_sha=baseline_sha, obj_key=obj_key,
        objective=target_metrics.get(obj_key),
        baseline_objective=baseline_metrics.get(obj_key),
        diff_path=diff_path, bundle_path=bundle_path,
    ), encoding="utf-8")

    branch = None
    if to_branch:
        _git(repo, "push", "origin", f"{target_sha}:refs/heads/{to_branch}")
        branch = to_branch

    return {
        "what": what,
        "sha": target_sha,
        "baseline_sha": baseline_sha,
        "objective_key": obj_key,
        "objective": target_metrics.get(obj_key),
        "baseline_objective": baseline_metrics.get(obj_key),
        "diff": str(diff_path),
        "bundle": str(bundle_path),
        "readme": str(md_path),
        "branch": branch,
        "source_repo": cfg["repo_path"],
    }


def _render_export_md(*, run_path: Path, cfg: dict, what: str, ref: str,
                      target_sha: str, target_desc: str, baseline_sha: str,
                      obj_key: str, objective, baseline_objective,
                      diff_path: Path, bundle_path: Path) -> str:
    objective_line = f"- objective `{obj_key}`: {objective}"
    if baseline_objective is not None:
        objective_line += f" (baseline: {baseline_objective})"
    return f"""# SimpleLoop export — {what}

- run_dir: `{run_path}`
- target: {target_desc}
- target sha: `{target_sha}`
- baseline sha: `{baseline_sha}` (`{cfg['baseline_ref']}`)
- source repo: `{cfg['repo_path']}`
{objective_line}

## Import into the source repo

As a branch (full commit chain, lossless):

```bash
cd {cfg['repo_path']}
git fetch {bundle_path} {ref}:refs/heads/simpleloop-{what}
```

As a squashed patch on the baseline:

```bash
cd {cfg['repo_path']}
git checkout {cfg['baseline_ref']}
git apply {diff_path}
```

Or run `simpleloop export --run-dir {run_path} --to-branch <name>` to push
`{target_sha[:12]}` straight into the source repo as a new branch.
"""
