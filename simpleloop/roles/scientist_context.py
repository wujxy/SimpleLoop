"""Tagged conversation spine for the Scientist lane.

Owns the raw ``messages`` list sent to the chat model PLUS a 1:1 parallel
``tags`` list (sidecar) that classifies each message by origin. The tags are
never sent to the provider — they drive context accounting (Phase 0), latest-
only state snapshots (Phase 1), and deterministic compaction (Phases 2/3).

Governing invariant: at every observable point,
``len(self.messages) == len(self.tags)``. ``_step`` (research_agent.py)
mutates ``messages`` directly on protocol-repair failures, so the caller must
``mark_step_start()`` before and ``reconcile_after_step()`` after each
``_step`` call to keep the sidecar honest; ``reconcile_after_step`` validates
the delta shape and fails loud (``ContextTagError``) rather than silently
mis-tagging.

Two principles govern every compaction (see the implementation plan):

* Committed vs uncommitted cognition — a ``scientific_state_snapshot`` is
  committed (durable); everything after the latest snapshot is uncommitted
  working cognition and must survive compaction until the model assimilates it.
* Compaction rebuilds the raw conversation only — it must never mutate the
  enclosing ``ScientistSessionState`` at the instant it runs.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- tag vocabulary -------------------------------------------------------

SEED = "seed"
ASSISTANT = "assistant"
TOOL_OBSERVATION = "tool_observation"
PROTOCOL_CORRECTION = "protocol_correction"
HISTORY_PACK = "history_pack"
STATE_SNAPSHOT = "scientific_state_snapshot"

_ALL_TAGS = (
    SEED, ASSISTANT, TOOL_OBSERVATION, PROTOCOL_CORRECTION,
    HISTORY_PACK, STATE_SNAPSHOT,
)


class ContextTagError(RuntimeError):
    """The message/tag sidecar could not be reconciled. Fail loud rather than
    silently corrupt accounting or compaction."""


@dataclass
class ContextPolicy:
    """Resolved Scientist working-context policy.

    Phase 1 (latest-only state snapshots) and Phase 2 (epistemic checkpoints at
    EXPLORE->NARROW / NARROW->DEEPEN) are ON by default — both validated on the
    proposer smoke benchmark (same seed: peak prompt-tokens 147K -> 81K, no
    proposal-quality regression). Set ``state_snapshot: append`` and/or
    ``epistemic_checkpoint: false`` under ``loop.context`` to opt back out for
    A/B comparison.
    """
    state_snapshot: str = "latest_only"       # "latest_only" | "append"
    epistemic_checkpoint: bool = True
    checkpoint_keep_pairs: int = 0
    emergency_threshold_tokens: int | None = None
    telemetry: bool = True
    # internal emergency defaults — deliberately not public config knobs
    emergency_window_pairs: int = 3
    emergency_window_max_chars: int = 24000

    @classmethod
    def from_config(cls, raw: object) -> "ContextPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("loop.context: must be an object")
        unknown = set(raw) - {
            "state_snapshot", "epistemic_checkpoint", "checkpoint_keep_pairs",
            "emergency_threshold_tokens", "telemetry",
        }
        if unknown:
            raise ValueError(f"loop.context: unknown key(s): {sorted(unknown)}")
        policy = cls()
        if "state_snapshot" in raw:
            value = raw["state_snapshot"]
            if value not in ("latest_only", "append"):
                raise ValueError(
                    "loop.context.state_snapshot: must be 'latest_only' or 'append'")
            policy.state_snapshot = value
        if "epistemic_checkpoint" in raw:
            policy.epistemic_checkpoint = bool(raw["epistemic_checkpoint"])
        if "checkpoint_keep_pairs" in raw:
            keep = raw["checkpoint_keep_pairs"]
            if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
                raise ValueError(
                    "loop.context.checkpoint_keep_pairs: must be an integer >= 0")
            policy.checkpoint_keep_pairs = keep
        if "emergency_threshold_tokens" in raw:
            threshold = raw["emergency_threshold_tokens"]
            if threshold is not None and (
                    not isinstance(threshold, int) or isinstance(threshold, bool)
                    or threshold < 1):
                raise ValueError(
                    "loop.context.emergency_threshold_tokens: "
                    "must be a positive integer or null")
            policy.emergency_threshold_tokens = threshold
        if "telemetry" in raw:
            policy.telemetry = bool(raw["telemetry"])
        return policy

    def to_dict(self) -> dict:
        return {
            "state_snapshot": self.state_snapshot,
            "epistemic_checkpoint": self.epistemic_checkpoint,
            "checkpoint_keep_pairs": self.checkpoint_keep_pairs,
            "emergency_threshold_tokens": self.emergency_threshold_tokens,
            "telemetry": self.telemetry,
        }


class ScientistConversation:
    """The Scientist's active conversation, tagged for accounting/compaction."""

    def __init__(self, *, policy: ContextPolicy):
        self.policy = policy
        self.messages: list[dict] = []
        self.tags: list[str] = []
        self.last_state_snapshot_index: int | None = None
        self._step_pre: int | None = None

    # -- integrity --------------------------------------------------------

    def _check(self) -> None:
        assert len(self.messages) == len(self.tags), (
            f"sidecar drift: {len(self.messages)} messages vs "
            f"{len(self.tags)} tags"
        )

    def _append(self, role: str, content: str, tag: str) -> None:
        self.messages.append({"role": role, "content": content})
        self.tags.append(tag)
        self._check()

    # -- builders (one per mutation site in run_lane) ---------------------

    def seed(self, text: str) -> None:
        self.messages = [{"role": "user", "content": text}]
        self.tags = [SEED]
        self.last_state_snapshot_index = None
        self._check()

    def reframe(self, text: str) -> None:
        """fresh_reframe: full wipe + reseed."""
        self.seed(text)

    def tool_observation(self, assistant_reply: str, observation_text: str) -> None:
        self._append("assistant", assistant_reply, ASSISTANT)
        self._append("user", observation_text, TOOL_OBSERVATION)

    def assistant(self, text: str) -> None:
        self._append("assistant", text, ASSISTANT)

    def history_pack(self, text: str) -> None:
        self._append("user", text, HISTORY_PACK)

    def state_snapshot(self, text: str) -> None:
        """Record a committed-state snapshot. Under ``latest_only`` the prior
        snapshot (if any) is removed first, so exactly one snapshot — the
        latest — is resident at any time. Full provenance stays in the trace."""
        if (self.policy.state_snapshot == "latest_only"
                and self.last_state_snapshot_index is not None):
            idx = self.last_state_snapshot_index
            del self.messages[idx]
            del self.tags[idx]
        self._append("user", text, STATE_SNAPSHOT)
        self.last_state_snapshot_index = len(self.messages) - 1

    # -- _step reconciliation --------------------------------------------

    def mark_step_start(self) -> None:
        """Call immediately before ``_step``. ``_step`` appends repair pairs
        to ``self.messages`` behind our back on protocol failure; we record
        the boundary so ``reconcile_after_step`` can classify the delta."""
        self._check()
        self._step_pre = len(self.messages)

    def reconcile_after_step(self) -> None:
        """Classify whatever ``_step`` appended. ``_step`` appends only on
        protocol-repair failure, always as ``[assistant, user-correction]``
        pairs; the successful reply is appended later by ``run_lane`` via
        ``assistant()``. Any other shape is a ``ContextTagError``."""
        if self._step_pre is None:
            raise ContextTagError(
                "reconcile_after_step() called without mark_step_start()")
        pre = self._step_pre
        self._step_pre = None
        delta = self.messages[pre:]
        if len(delta) % 2 != 0:
            raise ContextTagError(
                f"unexpected _step delta length {len(delta)}; expected even "
                "[assistant, correction] pairs")
        for offset in range(0, len(delta), 2):
            if delta[offset]["role"] != "assistant" or \
                    delta[offset + 1]["role"] != "user":
                raise ContextTagError(
                    f"unexpected _step delta roles at offset {offset}: "
                    f"{delta[offset]['role']}, {delta[offset + 1]['role']}")
        for offset in range(len(delta)):
            self.tags.append(
                ASSISTANT if offset % 2 == 0 else PROTOCOL_CORRECTION)
        self._check()

    # -- accounting (Phase 0) --------------------------------------------

    def account(self) -> dict:
        """Composition by tag: char totals + message counts. Char counts are a
        deterministic local estimator (relative units); the provider's
        ``usage.prompt_tokens`` is the absolute total."""
        chars = {tag: 0 for tag in _ALL_TAGS}
        counts = {tag: 0 for tag in _ALL_TAGS}
        for message, tag in zip(self.messages, self.tags):
            content = message.get("content") or ""
            chars[tag] += len(content)
            counts[tag] += 1
        return {"chars": chars, "messages": counts}

    def n_state_snapshots(self) -> int:
        return self.tags.count(STATE_SNAPSHOT)

    # -- compaction (Phases 2 & 3) ---------------------------------------

    def compact_checkpoint(self, *, keep_pairs: int) -> dict:
        """Epistemic checkpoint rebuild (EXPLORE->NARROW, NARROW->DEEPEN).

        Called right after a snapshot is appended, so the uncommitted tail is
        empty. Keep seed + history pack(s) + the latest snapshot; optionally
        retain the last ``keep_pairs`` (assistant, tool_observation) pairs that
        preceded the commit (default 0 — drop old scratch). The snapshot stays
        last so the model's final read is its current committed state."""
        self._check()
        seed_indices = [i for i, tag in enumerate(self.tags) if tag == SEED]
        history_indices = [i for i, tag in enumerate(self.tags) if tag == HISTORY_PACK]
        snap_idx = self.last_state_snapshot_index

        new_messages: list[dict] = []
        new_tags: list[str] = []
        if seed_indices:
            new_messages.append(self.messages[seed_indices[0]])
            new_tags.append(SEED)
        for i in history_indices:
            new_messages.append(self.messages[i])
            new_tags.append(HISTORY_PACK)
        if keep_pairs > 0 and snap_idx is not None:
            for msg, tag in self._recent_pairs_before(snap_idx, keep_pairs):
                new_messages.append(msg)
                new_tags.append(tag)
        if snap_idx is not None:
            new_messages.append(self.messages[snap_idx])
            new_tags.append(STATE_SNAPSHOT)

        self.messages = new_messages
        self.tags = new_tags
        self.last_state_snapshot_index = (
            len(self.messages) - 1 if snap_idx is not None else None
        )
        self._check()
        return {
            "kind": "checkpoint", "keep_pairs": keep_pairs,
            "n_messages_after": len(self.messages),
        }

    def compact_emergency(
        self, *, window_pairs: int | None = None,
        window_max_chars: int | None = None,
    ) -> dict:
        """Emergency rebuild — never discard unassimilated observations.

        Keep seed + history pack(s) + the latest snapshot + the FULL
        uncommitted tail (everything after the latest snapshot). Only if that
        tail alone exceeds the cap do we trim its OLDER part, always retaining
        at least the last complete (assistant, tool_observation) pair."""
        self._check()
        window_pairs = self.policy.emergency_window_pairs \
            if window_pairs is None else window_pairs
        window_max_chars = self.policy.emergency_window_max_chars \
            if window_max_chars is None else window_max_chars

        seed_indices = [i for i, tag in enumerate(self.tags) if tag == SEED]
        history_indices = [i for i, tag in enumerate(self.tags) if tag == HISTORY_PACK]
        snap_idx = self.last_state_snapshot_index
        tail_start = (snap_idx + 1) if snap_idx is not None else (
            1 if seed_indices else 0)
        tail = list(zip(self.messages[tail_start:], self.tags[tail_start:]))
        tail = self._cap_tail(tail, window_pairs, window_max_chars)

        new_messages: list[dict] = []
        new_tags: list[str] = []
        if seed_indices:
            new_messages.append(self.messages[seed_indices[0]])
            new_tags.append(SEED)
        for i in history_indices:
            new_messages.append(self.messages[i])
            new_tags.append(HISTORY_PACK)
        if snap_idx is not None:
            new_messages.append(self.messages[snap_idx])
            new_tags.append(STATE_SNAPSHOT)
            snap_new_index = len(new_messages) - 1
        else:
            snap_new_index = None
        for msg, tag in tail:
            new_messages.append(msg)
            new_tags.append(tag)

        self.messages = new_messages
        self.tags = new_tags
        self.last_state_snapshot_index = snap_new_index
        self._check()
        return {
            "kind": "emergency", "n_messages_after": len(self.messages),
            "tail_kept": len(tail),
        }

    # -- compaction helpers ----------------------------------------------

    def _recent_pairs_before(
        self, snap_idx: int, keep_pairs: int,
    ) -> list[tuple[dict, str]]:
        """The last ``keep_pairs`` (assistant, tool_observation) pairs strictly
        before ``snap_idx``, in chronological order with within-pair order
        preserved."""
        pairs: list[tuple[dict, dict]] = []  # most-recent first
        i = snap_idx - 1
        while i >= 1 and len(pairs) < keep_pairs:
            if self.tags[i] == TOOL_OBSERVATION and self.tags[i - 1] == ASSISTANT:
                pairs.append((self.messages[i - 1], self.messages[i]))
                i -= 2
            else:
                break
        pairs.reverse()  # chronological: oldest-kept pair first
        flat: list[tuple[dict, str]] = []
        for assistant_msg, tool_msg in pairs:
            flat.append((assistant_msg, ASSISTANT))
            flat.append((tool_msg, TOOL_OBSERVATION))
        return flat

    @staticmethod
    def _cap_tail(
        tail: list[tuple[dict, str]], window_pairs: int, window_max_chars: int,
    ) -> list[tuple[dict, str]]:
        """Keep the most recent whole units from ``tail`` until the cap bites,
        preserving chronological and within-pair order. The last unit is always
        retained: the cap checks are guarded by ``start`` still sitting on the
        sentinel, so the first (most-recent) unit is always admitted —
        guaranteeing the most-recent complete (assistant, tool_observation)
        pair survives even when the cap is smaller than a single pair."""
        if not tail:
            return tail
        chars = 0
        pairs_kept = 0
        start = len(tail)  # sentinel: nothing retained yet
        i = len(tail) - 1
        while i >= 0:
            if tail[i][1] == TOOL_OBSERVATION and i >= 1 and \
                    tail[i - 1][1] == ASSISTANT:
                pair_chars = (len(tail[i][0].get("content") or "")
                              + len(tail[i - 1][0].get("content") or ""))
                if start < len(tail) and (
                        pairs_kept >= window_pairs
                        or chars + pair_chars > window_max_chars):
                    break
                start = i - 1
                chars += pair_chars
                pairs_kept += 1
                i -= 2
            else:
                single_chars = len(tail[i][0].get("content") or "")
                if start < len(tail) and chars + single_chars > window_max_chars:
                    break
                start = i
                chars += single_chars
                i -= 1
        return tail[start:]
