"""Unit tests for the Scientist working-context wrapper.

Pure-Python: no model/runtime/Apptainer needed. Exercises the tagged spine,
Phase-0 accounting, Phase-1 latest-only snapshots, Phase-2 epistemic
checkpoints, and Phase-3 emergency compaction (including the uncommitted-tail
preservation rule).
"""
from __future__ import annotations

import pytest

from simpleloop.roles.scientist_context import (
    ASSISTANT, ContextPolicy, ContextTagError, HISTORY_PACK, PROTOCOL_CORRECTION,
    SEED, STATE_SNAPSHOT, ScientistConversation, TOOL_OBSERVATION,
)


def _policy(**overrides) -> ContextPolicy:
    base = dict(
        state_snapshot="append", epistemic_checkpoint=False,
        checkpoint_keep_pairs=0, emergency_threshold_tokens=None, telemetry=True,
    )
    base.update(overrides)
    # round-trip through from_config to mirror production resolution
    return ContextPolicy.from_config(base)


# --- ContextPolicy --------------------------------------------------------

def test_policy_defaults_have_phase1_and_phase2_on():
    pol = ContextPolicy.from_config(None)
    assert pol.state_snapshot == "latest_only"
    assert pol.epistemic_checkpoint is True
    assert pol.emergency_threshold_tokens is None
    assert pol.telemetry is True


def test_policy_rejects_unknown_keys():
    with pytest.raises(ValueError):
        ContextPolicy.from_config({"bogus": True})


def test_policy_validates_values():
    with pytest.raises(ValueError):
        ContextPolicy.from_config({"state_snapshot": "wrong"})
    with pytest.raises(ValueError):
        ContextPolicy.from_config({"checkpoint_keep_pairs": -1})
    with pytest.raises(ValueError):
        ContextPolicy.from_config({"emergency_threshold_tokens": 0})


# --- builders + sidecar integrity ----------------------------------------

def test_seed_and_tags_stay_in_sync():
    conv = ScientistConversation(policy=_policy())
    conv.seed("seed-text")
    assert len(conv.messages) == 1 == len(conv.tags)
    assert conv.tags == [SEED]
    conv.assistant("a1")
    conv.tool_observation("a2", "obs")
    assert conv.tags == [SEED, ASSISTANT, ASSISTANT, TOOL_OBSERVATION]
    assert len(conv.messages) == len(conv.tags)


def test_state_snapshot_append_keeps_history():
    conv = ScientistConversation(policy=_policy(state_snapshot="append"))
    conv.seed("s")
    conv.state_snapshot("v1")
    conv.assistant("a")
    conv.state_snapshot("v2")
    assert conv.tags.count(STATE_SNAPSHOT) == 2
    assert conv.n_state_snapshots() == 2
    assert conv.last_state_snapshot_index == 3


def test_state_snapshot_latest_only_replaces():
    conv = ScientistConversation(policy=_policy(state_snapshot="latest_only"))
    conv.seed("s")
    conv.state_snapshot("v1")
    conv.assistant("a")
    conv.tool_observation("b", "obs")  # uncommitted work between snapshots
    conv.state_snapshot("v2")
    # exactly one snapshot resident, and it is the latest, sitting last
    assert conv.n_state_snapshots() == 1
    assert conv.tags[-1] == STATE_SNAPSHOT
    assert conv.messages[-1]["content"] == "v2"
    # the working pairs between commits are preserved (Phase 1 only collapses snapshots)
    assert conv.tags.count(TOOL_OBSERVATION) == 1
    assert conv.last_state_snapshot_index == len(conv.messages) - 1


# --- reconcile_after_step -------------------------------------------------

def test_reconcile_classifies_repair_pairs():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    conv.mark_step_start()
    # simulate _step appending one repair pair behind the wrapper's back
    conv.messages.append({"role": "assistant", "content": "bad"})
    conv.messages.append({"role": "user", "content": "Protocol correction required ..."})
    conv.reconcile_after_step()
    assert conv.tags[-2:] == [ASSISTANT, PROTOCOL_CORRECTION]
    assert len(conv.messages) == len(conv.tags)


def test_reconcile_rejects_unexpected_shape():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    conv.mark_step_start()
    conv.messages.append({"role": "user", "content": "lone user"})  # odd delta
    with pytest.raises(ContextTagError):
        conv.reconcile_after_step()


def test_reconcile_requires_mark_first():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    with pytest.raises(ContextTagError):
        conv.reconcile_after_step()


# --- accounting -----------------------------------------------------------

def test_account_sums_chars_and_counts_by_tag():
    conv = ScientistConversation(policy=_policy())
    conv.seed("seed")
    conv.assistant("aaa")
    conv.tool_observation("bb", "cccc")
    account = conv.account()
    assert account["messages"][SEED] == 1
    assert account["messages"][ASSISTANT] == 2  # one assistant + the reply in tool_observation
    assert account["messages"][TOOL_OBSERVATION] == 1
    assert account["chars"][SEED] == len("seed")
    assert account["chars"][TOOL_OBSERVATION] == len("cccc")


# --- compact_checkpoint (Phase 2) ----------------------------------------

def test_checkpoint_drops_old_scratch_keeps_state_last():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    conv.tool_observation("a1", "big source dump 1")
    conv.tool_observation("a2", "big source dump 2")
    conv.history_pack("history")
    conv.state_snapshot("committed")
    # fire checkpoint with keep_pairs=0 (default at epistemic boundaries)
    event = conv.compact_checkpoint(keep_pairs=0)
    assert event["n_messages_after"] == 3  # seed + history + snapshot
    assert conv.tags == [SEED, HISTORY_PACK, STATE_SNAPSHOT]
    assert conv.messages[-1]["content"] == "committed"
    assert conv.last_state_snapshot_index == 2


def test_checkpoint_keep_pairs_retains_recent_scratch_before_snapshot():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    conv.tool_observation("a1", "old")
    conv.tool_observation("a2", "recent")
    conv.state_snapshot("committed")
    conv.compact_checkpoint(keep_pairs=1)
    assert conv.tags == [SEED, ASSISTANT, TOOL_OBSERVATION, STATE_SNAPSHOT]
    assert conv.messages[-2]["content"] == "recent"


# --- compact_emergency (Phase 3) -----------------------------------------

def test_emergency_preserves_uncommitted_tail():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    conv.state_snapshot("committed")
    # uncommitted working cognition after the snapshot — must survive
    conv.tool_observation("a1", "evidence A")
    conv.tool_observation("a2", "evidence B")
    event = conv.compact_emergency(window_pairs=3, window_max_chars=100000)
    assert event["tail_kept"] == 4  # both pairs intact
    assert conv.tags == [
        SEED, STATE_SNAPSHOT, ASSISTANT, TOOL_OBSERVATION,
        ASSISTANT, TOOL_OBSERVATION,
    ]


def test_emergency_caps_tail_but_keeps_last_pair():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    conv.state_snapshot("committed")
    conv.tool_observation("a1", "X" * 100)
    conv.tool_observation("a2", "Y" * 100)
    conv.tool_observation("a3", "Z" * 100)
    # tiny cap forces trimming, but the last complete pair must survive
    event = conv.compact_emergency(window_pairs=3, window_max_chars=150)
    assert event["tail_kept"] >= 2  # at least the last (assistant, tool_obs) pair
    assert conv.tags[-2:] == [ASSISTANT, TOOL_OBSERVATION]
    assert conv.messages[-1]["content"].startswith("Z")


def test_emergency_guarantees_last_pair_even_under_tight_cap():
    conv = ScientistConversation(policy=_policy())
    conv.seed("s")
    conv.state_snapshot("committed")
    conv.tool_observation("a1", "X" * 500)
    conv.tool_observation("a2", "Y" * 500)
    event = conv.compact_emergency(window_pairs=1, window_max_chars=10)
    # cap is smaller than any single pair, yet the last pair is retained
    assert event["tail_kept"] == 2
    assert conv.tags[-1] == TOOL_OBSERVATION
