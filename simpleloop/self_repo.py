"""Run-local self-repo lifecycle (RSI substrate).

The proposer package (the Scientist) is the "self" that RSI modifies. S3a makes the
run *actually execute* a per-run copy of it: at run start we snapshot the installed
``proposer/`` package into ``run_dir/self/repo/`` as an independent git history
(revision **S0**), and record the active self SHA in ``run_dir/self/state.json``. The
proposer-lane worker subprocess then resolves ``import proposer`` to that snapshot
(see ``proposer_lane_worker._redirect_self_repo``), so a one-line edit in the snapshot
takes effect in the next round while the installed package stays untouched.

Three physical layers (contract §2.4 / RSI impl-design §1):

    run_dir/self/state.json   Host-owned self state (Kernel-adjacent; proposer never writes it)
    run_dir/self/repo/        Body — the snapshotted proposer source, independent git history
    run_dir/self/reviews.jsonl (S3c, not created here)

S3a implements the lifecycle (snapshot S0 + state + fresh/continue). S3b adds the
viability authority: ``check_viability`` smoke-tests a candidate self by running it as a
normal task-mode proposer lane (the run's real goal + a throwaway empty workspace) and
checking it reaches a COMPLETED terminal — the loop contract in miniature, and the ONLY
adoption gate (RSI §8: protect the *loop*, not the *self*). Later slices grow this
module further: S3c mode switch + commitment, S3d ``transition`` (self-executor →
candidate → viability → adopt, advancing ``active_self_sha``).

This module is Host/Kernel code: it must never be importable from the ``proposer``
package, and never modified by a self-change. It deliberately does NOT import
``simpleloop.loop`` (matplotlib) so it stays light like ``proposer_lane_worker``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

# state.json schema. mode / next_self_review_round are written now and consumed only
# in S3c (mode switch + commitment scheduler) — frozen as part of the v0 schema
# (contract §14) so S3c needs no migration.
_SCHEMA_VERSION = 1
_DEFAULT_MODE = "task"
_DEFAULT_NEXT_REVIEW = None  # null until the first self-review sets a commitment (S3c)

# Smoke-test budget: a small episode is enough — we only need the candidate to reach a
# terminal (submit or abstain), not research depth. Model-dependent by design: the smoke
# test IS the loop contract in miniature (goal in -> result out), so it runs a real
# (short) research episode. See ``check_viability``.
_SMOKE_SCIENTIST_STEPS = 20
_SMOKE_TIMEOUT_SECONDS = 600


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SelfRepo:
    """Owns the run-local self-repo (Body) and its state record.

    ``self.repo`` is the git root and the ``sys.path`` entry the worker prepends:
    ``import proposer`` resolves to ``self.repo / "proposer"``.
    """

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "self"
        self.repo = self.root / "repo"
        self.state_path = self.root / "state.json"

    @staticmethod
    def _seed_dir() -> Path:
        """The proposer source to snapshot. Derived cwd-independently from this
        module's location: the repo root holds ``simpleloop/`` and ``proposer/`` as
        siblings, so the proposer package is ``<repo_root>/proposer``."""
        return Path(__file__).resolve().parent.parent / "proposer"

    # ---- lifecycle ----

    def setup(self, *, resume: bool) -> str:
        """Establish the active self for this run. Returns the active self SHA.

        Fresh run (``resume=False``): snapshot the seed proposer source into a new
        ``self/repo`` git history (S0) and write ``state.json``. If a ``self/`` tree
        already exists (leftover from an interrupted init — the existing-run guard in
        ``loop.run`` keys off ``history.jsonl``, so a crash before the first round can
        leave ``self/`` behind), it is removed first so fresh stays truly fresh.

        ``--continue`` (``resume=True``): reuse the existing self life-history — load
        ``active_self_sha`` from ``state.json`` and do NOT re-snapshot. If the state
        or repo is missing/inconsistent (e.g. an old pre-S3a run-dir carried forward),
        defensively re-snapshot S0 with a warning rather than crash.
        """
        if resume and self.state_path.exists() and self._is_git_repo(self.repo):
            sha = self._read_state()["active_self_sha"]
            print(f"[{_stamp()}] self-repo: resume at active_self_sha={sha[:10]} "
                  f"(S-life-history preserved, no re-snapshot)", flush=True)
            return sha
        if resume:
            print(f"[{_stamp()}] self-repo: --continue but state.json/self-repo "
                  f"missing or inconsistent — re-snapshotting S0", flush=True)
        return self._snapshot_s0()

    def _snapshot_s0(self) -> str:
        seed = self._seed_dir()
        if not seed.is_dir():
            raise FileNotFoundError(
                f"self-repo seed not found at {seed} (expected the proposer package "
                f"as a real directory next to simpleloop/)")
        # fresh = truly fresh: clear any interrupted-init leftover.
        if self.root.exists():
            shutil.rmtree(self.root)
        self.repo.mkdir(parents=True, exist_ok=False)

        # Body: a verbatim copy of the proposer source (no caches, no .git).
        shutil.copytree(
            seed, self.repo / "proposer",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        (self.repo / ".gitignore").write_text(
            "__pycache__/\n*.pyc\n", encoding="utf-8")

        # Independent git history rooted at self/repo. Local identity so commits never
        # depend on global git config (matters on compute nodes / fresh environments,
        # and for S3d's self-executor commits).
        self._git("init", "--quiet")
        self._git("config", "--local", "user.name", "SimpleLoop-RSI")
        self._git("config", "--local", "user.email", "rsi@simpleloop.local")
        self._git("add", "-A")
        self._git(
            "-c", "core.hooksPath=/dev/null",
            "commit", "--quiet", "-m", "S0: snapshot proposer")
        sha = self._git("rev-parse", "HEAD")

        self._write_state(sha)
        print(f"[{_stamp()}] self-repo: snapshotted proposer -> {self.repo} "
              f"(S0 = {sha[:10]})", flush=True)
        return sha

    # ---- state record ----

    @property
    def active_self_sha(self) -> str:
        return self._read_state()["active_self_sha"]

    @property
    def next_self_review_round(self):
        """The round at which the Host should next run a self-review (RSI S3c.2),
        or None if self-review is not scheduled. Set by the commitment scheduler."""
        return self._read_state().get("next_self_review_round")

    def update_commitment(self, *, next_self_review_round) -> None:
        """Write the next self-review commitment back to state.json, preserving
        the active self SHA + schema. (RSI S3c.2 — the Host兑现s the Scientist's
        own commitment; semantics §17: the Host is a clock, not a tutor.)"""
        state = self._read_state()
        self._write_state(
            state["active_self_sha"],
            next_self_review_round=next_self_review_round)

    def _read_state(self) -> dict:
        if not self.state_path.exists():
            raise FileNotFoundError(f"self state missing: {self.state_path}")
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise RuntimeError(
                f"self state schema_version mismatch in {self.state_path}: "
                f"{data.get('schema_version')} != {_SCHEMA_VERSION}")
        if not data.get("active_self_sha"):
            raise RuntimeError(f"self state has no active_self_sha: {self.state_path}")
        return data

    def _write_state(
        self, active_self_sha: str, *,
        next_self_review_round=_DEFAULT_NEXT_REVIEW,
        mode: str = _DEFAULT_MODE,
    ) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": _SCHEMA_VERSION,
            "active_self_sha": active_self_sha,
            "mode": mode,
            "next_self_review_round": next_self_review_round,
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    # ---- self-review ledger (RSI S3c.2) ------------------------------------
    # Host-owned, Host-appended; the proposer reads it read-only (contract §9.2 /
    # semantics §15). Mirrors harness/store.py:append_round's open-append-one-
    # line pattern (no tmp-replace, no per-write flock — the run-level flock at
    # loop._acquire_run_lock serializes).

    @property
    def reviews_path(self) -> Path:
        return self.root / "reviews.jsonl"

    def append_review(
        self, round_id: int, *, payload: dict, next_review_round: int,
        candidate_self_sha: str | None = None,
        viable: bool | None = None,
        adopted: bool | None = None,
    ) -> None:
        """Append one self-review record to run_dir/self/reviews.jsonl. For a
        CHANGE, S3d fills ``candidate_self_sha`` / ``viable`` / ``adopted`` from
        the transition outcome; for a KEEP they stay null. ``change`` is the
        payload's self_change (target/intent/instruction/evidence_refs) or None."""
        self.root.mkdir(parents=True, exist_ok=True)
        record = {
            "round": round_id,
            "incumbent_self_sha": payload.get("incumbent_self_sha"),
            "decision": payload.get("decision"),
            "diagnosis": payload.get("diagnosis"),
            "keep_reason": payload.get("keep_reason"),
            "change": payload.get("self_change"),
            "candidate_self_sha": candidate_self_sha,
            "viable": viable,
            "adopted": adopted,
            "next_review_round": next_review_round,
        }
        with self.reviews_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def last_review_round(self) -> int | None:
        """The round of the last recorded self-review, or None if none. Used by
        ``--continue`` resume (self-review rounds consume a round_id but write no
        history.jsonl line)."""
        path = self.reviews_path
        if not path.exists():
            return None
        last_round = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict) and isinstance(obj.get("round"), int):
                    last_round = obj["round"]
        except (OSError, json.JSONDecodeError):
            pass
        return last_round

    # ---- git helper (mirrors harness/workspace.py:_git) ----

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        return path.is_dir() and (path / ".git").exists()

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"self-repo git {' '.join(args)} failed: "
                f"{completed.stderr.strip()}")
        return completed.stdout.strip()

    @staticmethod
    def _git_at(path: str | Path, *args: str) -> str:
        """``git -C <path>`` for an arbitrary path (a self-repo worktree). The
        class's own ``_git`` operates on ``self.repo``; worktree ops need a
        different cwd."""
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"self-repo git {' '.join(args)} failed: "
                f"{completed.stderr.strip()}")
        return completed.stdout.strip()

    # ---- self-execution + adoption (S3d) ------------------------------------
    # The Host half of a CHANGE: open a throwaway worktree of self/repo at the
    # active SHA, let the self-executor edit it, commit a candidate, smoke-test it
    # (check_viability), and adopt (fast-forward self/repo's branch + advance
    # active_self_sha) or keep the incumbent. The active working tree is never
    # touched until adoption, so a failed/broken candidate cannot corrupt it.

    def _add_self_worktree(self, sha: str) -> Path:
        wt_root = self.root / "worktrees"
        wt_root.mkdir(parents=True, exist_ok=True)
        wt = wt_root / f"sr-{sha[:10]}"
        if wt.exists():
            shutil.rmtree(wt)
        self._git("worktree", "prune")  # drop stale admin metadata
        completed = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "--detach",
             str(wt), sha],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"self-repo worktree add failed: {completed.stderr.strip()}")
        return wt

    def _self_worktree_has_changes(self, wt: Path) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        return bool(completed.stdout.strip())

    def _commit_self_worktree(self, wt: Path, message: str) -> str:
        self._git_at(wt, "add", "-A")
        self._git_at(
            wt, "-c", "user.name=SimpleLoop-RSI",
            "-c", "user.email=rsi@simpleloop.local",
            "commit", "--quiet", "-m", message)
        return self._git_at(wt, "rev-parse", "HEAD")

    def _remove_self_worktree(self, wt: Path) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(wt)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _adopt(self, candidate_sha: str) -> None:
        """Fast-forward self/repo's branch to the candidate and record it in
        state.json (preserving the commitment). The candidate descends from the
        active SHA, so ``merge --ff-only`` is always a fast-forward."""
        self._git("merge", "--ff-only", candidate_sha)
        state = self._read_state()
        self._write_state(
            candidate_sha,
            next_self_review_round=state.get("next_self_review_round"))

    def transition(self, *, agent, self_change: dict, run_dir: str | Path,
                   label: str = "self-exec") -> "TransitionResult":
        """Apply a self_change, smoke-test the candidate, adopt if viable (S3d).

        The self-executor (``agent`` — a claude-p ``Agent`` reused from the task
        executor) edits a throwaway worktree of self/repo at the active SHA under a
        contained mount (only ``proposer/`` writable; everything else read-only).
        On any failure — no instruction, executor error, no change, or a non-viable
        candidate — the incumbent self is preserved untouched. Returns the outcome
        for the reviews.jsonl ledger. See ``check_viability`` for the one gate.
        """
        sc = self_change or {}
        instruction = sc.get("instruction")
        if not instruction:
            return TransitionResult(None, False, False, "self_change has no instruction")
        old_sha = self.active_self_sha
        wt = self._add_self_worktree(old_sha)
        try:
            try:
                executor = agent(wt) if callable(agent) else agent
                executor.run_text(_self_exec_prompt(sc), cwd=wt, label=label)
            except Exception as exc:  # noqa: BLE001 — any executor failure keeps incumbent
                return TransitionResult(None, False, False, f"executor failed: {exc}")
            if not self._self_worktree_has_changes(wt):
                return TransitionResult(None, False, False, "executor made no changes")
            target = sc.get("target") or "self-change"
            candidate = self._commit_self_worktree(wt, f"S(n+1): {target}")
            vr = check_viability(wt, run_dir)
            if vr.viable:
                self._adopt(candidate)
                return TransitionResult(candidate, True, True, vr.detail)
            return TransitionResult(candidate, False, False, vr.detail)
        finally:
            self._remove_self_worktree(wt)


# ---- viability authority (S3b, revised: behavior-level smoke test) --------
# The ONLY adoption gate, and it protects the LOOP, not the SELF. The question is not
# "is this a healthy/contract-conforming self" (that would cage evolution) but "can
# this self still participate in the loop" — i.e. given a goal + a workspace, does it
# still emit a valid lane result? Behavior-level, not schema-level: the candidate is
# free to change anything — contract version, continuity format, output schema, even
# its own control loop — as long as the externally observable behavior (goal in ->
# result out) still holds. If a change breaks that, the smoke run produces no result
# (boot/import death) or a LANE_FAILED (research() raised) and the candidate is
# rejected; a clean submit OR abstain both pass — abstention is normal loop
# participation, and the self is free to degrade itself (the task signal, not the
# harness, teaches it to recover). Not wired into adoption until S3d.


@dataclass(frozen=True)
class ViabilityResult:
    """Outcome of a viability smoke-test on a candidate self-repo.

    ``viable`` is the single boolean the adoption authority cares about; ``detail``
    carries the verdict reason (COMPLETED outcome + proposal count, or why it failed)
    for the adoption ledger / diagnostics.
    """

    viable: bool
    detail: str


def _classify_smoke(result: dict | None, *, exit_code: int | None,
                    stderr_tail: str = "") -> ViabilityResult:
    """Pure verdict on a smoke run's ``result.json`` (or its absence).

    - no result / not a dict -> not viable: the worker died before writing one, i.e.
      the candidate proposer package failed to boot/import (a SyntaxError / ImportError
      at module load — the process exits before ``main()`` ever runs).
    - ``status == "COMPLETED"`` -> viable: research() reached a terminal (submit or
      abstain). Both mean the candidate can participate in the loop.
    - any other status (``LANE_FAILED`` …) -> not viable: research() raised.

    Factored out so the verdict logic is unit-testable without spawning a subprocess.
    """
    if not isinstance(result, dict):
        tail = f"\n--- worker stderr tail ---\n{stderr_tail}" if stderr_tail else ""
        return ViabilityResult(
            False,
            f"smoke worker exited rc={exit_code} without writing a result — "
            f"the candidate proposer failed to boot/import{tail}")
    status = result.get("status")
    if status == "COMPLETED":
        return ViabilityResult(
            True,
            f"smoke COMPLETED (outcome={result.get('outcome')}, "
            f"n_proposals={len(result.get('proposals') or [])})")
    tail = f"\n--- worker stderr tail ---\n{stderr_tail}" if stderr_tail else ""
    return ViabilityResult(
        False,
        f"smoke status={status}: research() raised — "
        f"{result.get('explanation') or result.get('abstain_reason')}{tail}")


def _tail(path: Path, n: int = 2000) -> str:
    """Last ~n chars of a file (the worker's job.err), for failed-candidate diagnostics."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def _empty_git_workspace(path: Path) -> str:
    """A valid-but-empty git repo with one initial commit, so the candidate's research
    probes (``git log``/``show``/…) don't fail for *environmental* reasons and falsely
    reject a healthy self. Returns the empty-commit SHA (a usable ``base_sha``)."""
    for args in (
        ["git", "-C", str(path), "init", "--quiet"],
        ["git", "-C", str(path), "config", "--local", "user.name", "smoke"],
        ["git", "-C", str(path), "config", "--local", "user.email", "smoke@local"],
        ["git", "-C", str(path), "commit", "--quiet", "--allow-empty", "-m", "smoke"],
    ):
        subprocess.run(args, check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         text=True, stdout=subprocess.PIPE, check=False)
    return out.stdout.strip() or ""


def check_viability(candidate_repo: str | Path, run_dir: str | Path) -> ViabilityResult:
    """Smoke-test a candidate self-repo: run it as a normal task-mode proposer lane on
    the run's real goal + a throwaway empty workspace, and return whether it reached a
    COMPLETED terminal. See the module/section notes for the semantics.

    Isolation: the smoke run gets a TEMP run_dir carrying only a copy of the real
    run's ``config.resolved.json`` (so goal / runtime / repo paths are real) but FRESH
    proposer memory and an EMPTY workspace — it never writes to the real run's proposer
    state. ``candidate_repo`` is loaded via the worker's ``--self-repo`` redirect.
    """
    run_dir = Path(run_dir)
    resolved = run_dir / "config.resolved.json"
    if not resolved.is_file():
        return ViabilityResult(
            False, f"no config.resolved.json in {run_dir} — cannot smoke-test")

    candidate = str(Path(candidate_repo).resolve())
    with TemporaryDirectory() as base:
        smoke_run, result_dir = Path(base), Path(base) / "result"
        result_dir.mkdir()
        shutil.copy2(resolved, smoke_run / "config.resolved.json")
        smoke_ws = smoke_run / "ws"
        smoke_ws.mkdir()
        base_sha = _empty_git_workspace(smoke_ws)
        # Mirrors ProposerLaneSpec.to_dict() (mode="task"); the worker reads it via
        # ProposerLaneSpec.from_dict, which tolerates extra/missing fields.
        manifest = {
            "lane_id": 0, "round_id": 0, "base_sha": base_sha,
            "run_dir": str(smoke_run), "workspace_path": str(smoke_ws),
            "result_dir": str(result_dir), "prompt_dir": "",
            "proposal_slots": 1, "scientist_steps": _SMOKE_SCIENTIST_STEPS,
            "attempt": 1, "mode": "task",
        }
        (result_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        argv = [sys.executable, "-m", "simpleloop.proposer_lane_worker",
                "--self-repo", candidate,
                "--manifest", str(result_dir / "manifest.json")]
        err_path = result_dir / "job.err"
        with open(err_path, "w") as err:
            try:
                proc = subprocess.run(
                    argv, stdout=subprocess.DEVNULL, stderr=err,
                    check=False, timeout=_SMOKE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                return ViabilityResult(
                    False, f"smoke run timed out after {_SMOKE_TIMEOUT_SECONDS}s")

        result = None
        rpath = result_dir / "result.json"
        if rpath.is_file():
            try:
                decoded = json.loads(rpath.read_text(encoding="utf-8"))
                result = decoded if isinstance(decoded, dict) else None
            except (OSError, json.JSONDecodeError):
                result = None
        return _classify_smoke(
            result, exit_code=proc.returncode, stderr_tail=_tail(err_path))


# ---- self-execution + adoption (S3d) --------------------------------------
# TransitionResult + the self-executor prompt. The transition logic itself lives
# on SelfRepo (it owns the self-repo git lifecycle and calls check_viability);
# this is the result type it returns and the prompt it feeds the reused executor.


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a self-change transition (S3d). ``candidate_self_sha`` is None
    when the executor produced no usable change (no instruction / error / no
    edit); ``viable``/``adopted`` reflect the smoke test + adoption decision."""

    candidate_self_sha: str | None
    viable: bool
    adopted: bool
    detail: str


def _self_exec_prompt(self_change: dict) -> str:
    """Build the self-executor's prompt from a self_change (target/intent/
    instruction). The executor edits ``proposer/`` in its worktree; the writable
    boundary is enforced by the contained mount (only proposer/ rw), not by this
    text — maximum freedom, the smoke test is the only gate."""
    sc = self_change or {}
    return f"""You are modifying the Scientist — the ``proposer/`` package, your own source.
A self-review diagnosed that the "{sc.get('target') or '?'}" layer limits progress toward the Goal.

Intent:
{sc.get('intent') or ''}

Change to make:
{sc.get('instruction') or ''}

Edit the ``proposer/`` source files in this worktree. Everything outside it is
read-only; do not run git or touch ``.git`` — the harness inspects and commits your
file changes. When the edit is complete, stop.
"""
