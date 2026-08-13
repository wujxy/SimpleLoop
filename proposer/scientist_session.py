"""Persistent Scientist session — the continuity layer for one Scientist.

Three stores under ``run_dir/scientists/lane-<N>/``:

  - ``session.jsonl`` — append-only archive of every conversation message this
    Scientist ever produced or observed (assistant replies, tool observations,
    world events). The immutable ground-truth record of what happened.
  - ``notebook.md``  — the Scientist's own running self-account, rewritten at
    each suspension checkpoint. REVISABLE AUTOBIOGRAPHICAL MEMORY: not an
    instruction, not established fact; a lossy long-term self-model that may
    lag, oversimplify, or be wrong. When it disagrees with the live workspace
    or the experiment records, the records win.
  - ``meta.json``    — stable ``scientist_id`` (identity, NOT the lane scheduling
    position), prompt version, last base_sha, last round.

The lane directory is a LOCATOR (where this lane's resident scientist lives);
the ``scientist_id`` inside is the IDENTITY (who). Single-lane today. True
multi-lane provenance — stamping ``scientist_id`` onto candidates in
history.jsonl so a Scientist can be told "your proposal → this outcome" when
other Scientists also ran — is deferred; it requires touching the loop.py /
store.py seam that this refactor deliberately freezes. For now all of a round's
experiments are attributed to the resident Scientist, which is correct under
the single hardcoded lane. See docs/scientist-refactor-plan.md §Revision.5.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def _new_scientist_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ScientistSession:
    """One Scientist's persistent state across rounds.

    ``trajectory`` is the full prior conversation (flat ``[{role, content}]``),
    loaded from session.jsonl. ``tail_turns`` extracts recent complete
    (assistant → user-observation) turn-blocks for the resume context.
    """

    session_dir: Path
    scientist_id: str
    notebook: str
    trajectory: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    _prompt_version: str = ""

    @property
    def session_path(self) -> Path:
        return self.session_dir / "session.jsonl"

    @property
    def notebook_path(self) -> Path:
        return self.session_dir / "notebook.md"

    @property
    def meta_path(self) -> Path:
        return self.session_dir / "meta.json"

    @classmethod
    def load_or_create(
        cls,
        run_dir: Path,
        lane_id: int,
        *,
        prompt_version: str,
    ) -> "ScientistSession":
        """Load an existing resident Scientist for this lane, or initialize a
        fresh one (new scientist_id, empty notebook, empty trajectory)."""
        session_dir = Path(run_dir) / "scientists" / f"lane-{int(lane_id)}"
        session_dir.mkdir(parents=True, exist_ok=True)

        meta: dict = {}
        if session_dir.joinpath("meta.json").exists():
            try:
                loaded = json.loads(
                    session_dir.joinpath("meta.json").read_text(encoding="utf-8")
                )
                if isinstance(loaded, dict):
                    meta = loaded
            except (OSError, json.JSONDecodeError):
                meta = {}

        loaded_id = str(meta.get("scientist_id") or "").strip()
        scientist_id = loaded_id or _new_scientist_id()
        # Persist a fresh scientist_id IMMEDIATELY — before any round runs — so
        # a worker killed mid-first-round does not leave session.jsonl orphaned
        # from its identity (the archive would otherwise load under a new UUID:
        # the same lived trajectory with a different ID card).
        if not loaded_id:
            meta = {
                "scientist_id": scientist_id,
                "prompt_version": prompt_version,
                "created_round": None,
                "last_round": None,
                "last_base_sha": None,
            }
            session_dir.joinpath("meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        trajectory: list[dict] = []
        if session_dir.joinpath("session.jsonl").exists():
            for line in session_dir.joinpath("session.jsonl").read_text(
                encoding="utf-8"
            ).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("role")
                content = obj.get("content")
                if isinstance(role, str) and isinstance(content, str):
                    trajectory.append({"role": role, "content": content})

        notebook = ""
        if session_dir.joinpath("notebook.md").exists():
            notebook = session_dir.joinpath("notebook.md").read_text(encoding="utf-8")

        return cls(
            session_dir=session_dir,
            scientist_id=scientist_id,
            notebook=notebook,
            trajectory=trajectory,
            meta=meta,
            _prompt_version=prompt_version,
        )

    def is_first_round(self) -> bool:
        """True when this Scientist has no lived trajectory and no notebook —
        an honest cold start (round 0 of this Scientist's life on the problem)."""
        return not self.trajectory and not self.notebook.strip()

    def append_message(
        self, role: str, content: str, *, round_id: int | None = None
    ) -> None:
        """Append one message to the immutable archive AND the in-memory
        trajectory. Called live as the Scientist's round proceeds."""
        with self.session_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"role": role, "content": content, "round": round_id},
                    ensure_ascii=False,
                )
                + "\n"
            )
        self.trajectory.append({"role": role, "content": content})

    def write_notebook(self, text: str) -> None:
        """Replace the notebook with a fresh self-account (the notebook is
        revised, not appended — it is a living self-model, not a log)."""
        self.notebook_path.write_text(text, encoding="utf-8")
        self.notebook = text

    def save_meta(self, *, round_id: int, base_sha: str) -> None:
        self.meta["scientist_id"] = self.scientist_id
        self.meta["prompt_version"] = self._prompt_version
        if "created_round" not in self.meta or self.meta["created_round"] is None:
            self.meta["created_round"] = round_id
        self.meta["last_round"] = round_id
        self.meta["last_base_sha"] = base_sha
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def tail_turns(self, max_blocks: int) -> list[dict]:
        """Return up to ``max_blocks`` complete turn-blocks from the end of the
        trajectory, flattened back to ``[{role, content}, ...]``.

        A turn-block is an assistant message followed by its user observation.
        Truncating on block boundaries prevents orphaning an observation from
        the action that produced it (which would leave the resumed Scientist
        staring at a result it cannot remember wanting). Leading user seed
        messages (the cold-start / resume notices) are not part of any block and
        are excluded from the tail — they are superseded by the notebook and the
        fresh world event injected this round.
        """
        if max_blocks <= 0:
            return []
        blocks: list[tuple[dict, dict]] = []
        i = 0
        msgs = self.trajectory
        while i < len(msgs):
            cur = msgs[i]
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            if (
                cur.get("role") == "assistant"
                and nxt is not None
                and nxt.get("role") == "user"
            ):
                blocks.append((cur, nxt))
                i += 2
            else:
                i += 1
        if not blocks:
            return []
        kept = blocks[-max_blocks:]
        out: list[dict] = []
        for assistant, user in kept:
            out.append(assistant)
            out.append(user)
        return out
