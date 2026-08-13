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
viability authority: ``check_viability`` spawns ``proposer_lane_worker --check`` on a
candidate self to prove it can still boot/load-continuity/produce-output offline (RSI
§19: viability is immediate). Later slices grow this module further: S3c mode switch +
commitment, S3d ``transition`` (self-executor → candidate → viability → adopt, advancing
``active_self_sha``).

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

# state.json schema. mode / next_self_review_round are written now and consumed only
# in S3c (mode switch + commitment scheduler) — frozen as part of the v0 schema
# (contract §14) so S3c needs no migration.
_SCHEMA_VERSION = 1
_DEFAULT_MODE = "task"
_DEFAULT_NEXT_REVIEW = None  # null until the first self-review sets a commitment (S3c)

# The single proposer-CLI contract version the Host speaks. The proposer declares its
# own side as ``proposer.CONTRACT_VERSION``; viability (``check_viability``) asserts
# they match. Changing this is a Kernel change (contract §11), not a self-modification.
EXPECTED_CONTRACT_VERSION = "proposer-cli-v0"
# Hard ceiling for the offline viability subprocess (it never calls the model).
_VIABILITY_TIMEOUT_SECONDS = 120


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
    # semantics §15). Mirrors harness/store.py:append_generation's open-append-one-
    # line pattern (no tmp-replace, no per-write flock — the run-level flock at
    # loop._acquire_run_lock serializes).

    @property
    def reviews_path(self) -> Path:
        return self.root / "reviews.jsonl"

    def append_review(
        self, round_id: int, *, payload: dict, next_review_round: int,
    ) -> None:
        """Append one self-review record to run_dir/self/reviews.jsonl. The S3d
        fields (candidate_self_sha / viable / adopted) are null until adoption
        exists; ``change`` is the payload's self_change (target/intent/
        instruction/evidence_refs) or None for KEEP."""
        self.root.mkdir(parents=True, exist_ok=True)
        record = {
            "round": round_id,
            "incumbent_self_sha": payload.get("incumbent_self_sha"),
            "decision": payload.get("decision"),
            "diagnosis": payload.get("diagnosis"),
            "keep_reason": payload.get("keep_reason"),
            "change": payload.get("self_change"),
            "candidate_self_sha": None,   # S3d
            "viable": None,               # S3d
            "adopted": None,              # S3d
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


# ---- viability authority (S3b) -----------------------------------------
# Not yet wired into adoption — S3d's transition authority will call this on a
# self-executor candidate before advancing active_self_sha. Built + verified in
# isolation here.


@dataclass(frozen=True)
class ViabilityResult:
    """Outcome of a viability check on a candidate self-repo.

    ``viable`` is the single boolean the adoption authority cares about; ``stderr``
    carries the worker's ``[check] FAIL: …`` lines (or an import-time traceback for a
    boot failure) for diagnostics.
    """

    viable: bool
    exit_code: int
    stdout: str
    stderr: str


def check_viability(candidate_repo: str | Path, run_dir: str | Path) -> ViabilityResult:
    """Spawn ``proposer_lane_worker --check`` on a candidate self-repo and return its
    verdict. The worker redirects ``import proposer`` to ``candidate_repo`` (via
    ``--self-repo``) and runs the offline assertions (boot / contract version /
    continuity load / protocol produce). Exit 0 = viable; non-zero = broken self.

    Offline + deterministic by construction: the check subprocess never calls the
    model (it only exercises import + continuity read + data-model construction), so
    no network is needed. ``run_dir`` supplies the incumbent continuity the candidate
    must still be able to read.
    """
    argv = [
        sys.executable, "-m", "simpleloop.proposer_lane_worker",
        "--check",
        "--self-repo", str(candidate_repo),
        "--run-dir", str(run_dir),
        "--contract-version", EXPECTED_CONTRACT_VERSION,
    ]
    try:
        proc = subprocess.run(
            argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=_VIABILITY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return ViabilityResult(
            viable=False, exit_code=-1, stdout="",
            stderr=f"viability check timed out after {_VIABILITY_TIMEOUT_SECONDS}s: "
                   f"{exc}",
        )
    return ViabilityResult(
        viable=proc.returncode == 0,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
