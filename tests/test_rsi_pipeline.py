from __future__ import annotations

from pathlib import Path, PurePosixPath

from simpleloop.rsi.history import (
    JsonlSelfHistoryStore,
    candidate_created_event,
    reviewed_change_event,
)
from simpleloop.rsi.models import (
    RsiRequest,
    SelfCandidate,
    SelfChange,
    SelfDecision,
    SelfDecisionKind,
    SelfEditResult,
    SelfEventKind,
    SelfRevision,
    ViabilityResult,
)
from simpleloop.rsi.pipeline import RsiPipeline, run_rsi
from simpleloop.world import SourceWorkspace


class Reviewer:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def review(self, request):
        self.calls.append(request)
        return self.decision


class Editor:
    def __init__(self, result=None):
        self.result = result or SelfEditResult("EDITED", "done")
        self.calls = []

    def edit(self, request):
        self.calls.append(request)
        return self.result


class Bodies:
    def __init__(self, root: Path):
        self.root = root
        self.workspace = SourceWorkspace("self-4", root / "candidate", "s0")
        self.workspace.path.mkdir(parents=True)
        self.prepared = []
        self.committed = []
        self.materialized = []
        self.discarded = []

    def initialize(self, seed):
        return SelfRevision("s0")

    def prepare_candidate(self, round_id, parent_sha):
        self.prepared.append((round_id, parent_sha))
        return SourceWorkspace(f"self-{round_id}", self.workspace.path, parent_sha)

    def commit_candidate(self, workspace, request):
        self.committed.append((workspace, request))
        return SelfCandidate(request.parent_sha, "s1", (PurePosixPath("prompt.py"),))

    def discard_candidate(self, workspace):
        self.discarded.append(workspace)

    def materialize(self, sha):
        self.materialized.append(sha)
        return self.root / "runtime"


class Viability:
    def __init__(self, viable=True):
        self.result = ViabilityResult(viable, "smoke")
        self.calls = []

    def check(self, request):
        self.calls.append(request)
        return self.result


class Checkpoint:
    def __init__(self, record=None):
        self.record = record
        self.cleared = 0

    def inflight(self):
        return self.record

    def clear(self):
        self.cleared += 1
        self.record = None


def keep(defer=5):
    return SelfDecision(
        SelfDecisionKind.KEEP,
        "progress sufficient",
        keep_reason="strong progress",
        next_review_after_rounds=defer,
    )


def change():
    return SelfDecision(
        SelfDecisionKind.CHANGE,
        "search repeats",
        change=SelfChange("prompt", "broaden", "edit charter"),
    )


def ports(tmp_path, decision=None, viable=True, edit=None):
    history = JsonlSelfHistoryStore(tmp_path / "self")
    history.initialize("s0", 4)
    return {
        "reviewer": Reviewer(decision or change()),
        "editor": Editor(edit),
        "bodies": Bodies(tmp_path),
        "viability": Viability(viable),
        "history": history,
    }


def test_keep_appends_one_terminal_event_and_advances_commitment(tmp_path):
    values = ports(tmp_path, keep())

    result = run_rsi(RsiRequest(4, "goal"), **values)

    assert result.decision is SelfDecisionKind.KEEP
    assert values["history"].state().next_review_round == 9
    assert values["editor"].calls == []


def test_change_edits_commits_checks_and_adopts(tmp_path):
    values = ports(tmp_path)

    result = run_rsi(RsiRequest(4, "goal"), **values)

    assert result.adopted is True
    assert values["history"].state().active_sha == "s1"
    assert [event.kind for event in values["history"].events()][-3:] == [
        SelfEventKind.REVIEWED_CHANGE,
        SelfEventKind.CANDIDATE_CREATED,
        SelfEventKind.CANDIDATE_ADOPTED,
    ]
    assert values["bodies"].materialized == ["s0", "s1"]


def test_nonviable_candidate_is_rejected_and_incumbent_stays_active(tmp_path):
    values = ports(tmp_path, viable=False)

    result = run_rsi(RsiRequest(4, "goal"), **values)

    assert result.adopted is False
    assert values["history"].state().active_sha == "s0"
    assert values["history"].events()[-1].kind is SelfEventKind.CANDIDATE_REJECTED


def test_editor_failure_is_a_terminal_rejection_without_viability(tmp_path):
    values = ports(
        tmp_path,
        edit=SelfEditResult("EDITOR_FAILED", reason="model unavailable"),
    )

    result = run_rsi(RsiRequest(4, "goal"), **values)

    assert result.adopted is False
    assert "model unavailable" in result.detail
    assert values["bodies"].committed == []
    assert values["viability"].calls == []


def test_resume_after_candidate_created_skips_review_edit_and_commit(tmp_path):
    values = ports(tmp_path)
    history = values["history"]
    history.append(reviewed_change_event(4, "s0", change()))
    history.append(candidate_created_event(4, "s0", "s1", "prompt.py"))

    result = run_rsi(RsiRequest(4, "goal"), **values)

    assert result.adopted is True
    assert values["reviewer"].calls == []
    assert values["editor"].calls == []
    assert values["bodies"].committed == []
    assert len(values["viability"].calls) == 1


def test_repeated_terminal_call_does_not_repeat_ports(tmp_path):
    values = ports(tmp_path, keep())
    first = run_rsi(RsiRequest(4, "goal"), **values)
    second = run_rsi(RsiRequest(4, "goal"), **values)

    assert second == first
    assert len(values["reviewer"].calls) == 1


def test_pipeline_prepare_clears_only_terminal_stale_checkpoint(tmp_path):
    values = ports(tmp_path, keep())
    run_rsi(RsiRequest(4, "goal"), **values)
    record = type("Record", (), {"stage": "self_review", "round_id": 4})()
    checkpoint = Checkpoint(record)
    pipeline = RsiPipeline(
        goal="goal",
        seed=tmp_path / "seed",
        first_review_round=4,
        checkpoint=checkpoint,
        **{key: values[key] for key in (
            "reviewer", "editor", "bodies", "viability", "history"
        )},
    )

    pipeline.prepare()

    assert checkpoint.cleared == 1
    assert pipeline.due(9) is True
