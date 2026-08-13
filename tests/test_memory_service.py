"""Tests for MemoryService target resolution + experiment linking + tools."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from proposer.memory import MemoryService
from proposer.memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)


METRICS = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
           "gates": []}


def _write_history(run_dir: Path, *records: dict) -> None:
    path = run_dir / "history.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def _round(round_id: int, *cands: dict) -> dict:
    return {
        "round": round_id,
        "parent_sha": "p",
        "candidates": list(cands),
    }


def _cand(cid: int, *, fid: str | None = None,
          gate: bool = True, sel: bool = False,
          objective: float = 100.0,
          paths=("src/a.cc",),
          proposal: str = "prop") -> dict:
    return {
        "candidate": cid,
        "experiment_id": f"r0c{cid}",
        "finding_id": fid,
        "proposal": proposal,
        "parent_sha": "p",
        "sha": f"sha-{cid}",
        "status": "COMPLETED",
        "eval_block": "",
        "metrics": {"SPEED_MS": objective},
        "changed_paths": list(paths),
        "gates": {},
        "gate_passed": gate,
        "eligible": gate,
        "selected": sel,
    }


def test_resolve_new_target_allocates_finding_id_and_persists(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    proposals = [
        ResearchProposal(
            instruction="Hoist QPDF lookup.",
            research_target=NewFindingTarget(question="Is QPDF the cost?"),
        ),
        ResearchProposal(
            instruction="Pack cache values.",
            research_target=NewFindingTarget(
                question="Does packing help?",
                mechanisms=("cache", "layout"),
                code_regions=("OMILREC/src",),
            ),
        ),
    ]
    ids = svc.resolve_targets(proposals, round_id=0)
    assert ids == ["F-001", "F-002"]
    stored = svc.finding_store.load_all()
    assert stored["F-001"].state == "open"
    assert stored["F-002"].mechanisms == ("cache", "layout")


def test_resolve_existing_target_requires_known_finding(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    with pytest.raises(ValueError, match="unknown finding"):
        svc.resolve_targets(
            [ResearchProposal(
                instruction="continue F-999",
                research_target=ExistingFindingTarget(finding_id="F-999"),
            )],
            round_id=0,
        )


def test_commit_proposals_records_refs_and_derives_stats(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    proposals = [
        ResearchProposal(
            instruction="First look.",
            research_target=NewFindingTarget(question="Q?"),
        ),
    ]
    # commit at emit: records the predicted ref r0c0 (intent); no outcomes yet.
    [fid] = svc.commit_proposals(round_id=0, proposals=proposals)
    committed = svc.finding_store.load_all()[fid]
    assert committed.state == "active"
    assert committed.experiment_refs == ("r0c0",)
    assert committed.last_touched_round == 0
    # Before history records: the ref is predicted-not-yet-run -> zero stats.
    pre = svc.inspect_finding(fid)["stats"]
    assert pre == {"attempts": 0, "eligible": 0, "selected": 0,
                   "best_objective": None}
    # After history records the outcome: derived stats join it (read-only).
    _write_history(tmp_path, _round(0, _cand(0, fid=fid, objective=95.0,
                                             sel=True)))
    derived = svc.inspect_finding(fid)["stats"]
    assert derived["attempts"] == 1
    assert derived["eligible"] == 1
    assert derived["selected"] == 1
    assert derived["best_objective"] == 95.0


def test_commit_proposals_scrub_is_idempotent_on_retry(tmp_path: Path):
    """A retried proposer lane must not leave two findings (or double refs)
    claiming the same slot — commit_proposals scrubs the round's slot refs
    first, so the last commit wins and _derive_stats does not double-count."""
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    proposals = [ResearchProposal(
        instruction="x", research_target=NewFindingTarget(question="Q?"))]
    # First (crashed) commit allocates F-001 with r0c0.
    svc.commit_proposals(round_id=0, proposals=proposals)
    # Retry: commit again for the same round/slot. Scrub clears r0c0 from
    # F-001 first, then resolve allocates F-002 (NewFindingTarget doesn't
    # dedup on question), and records r0c0 on F-002.
    [fid2] = svc.commit_proposals(round_id=0, proposals=proposals)
    findings = svc.finding_store.load_all()
    assert fid2 == "F-002"
    # F-001's r0c0 was scrubbed; only F-002 claims it -> no double-count.
    assert "r0c0" not in findings["F-001"].experiment_refs
    assert findings["F-002"].experiment_refs == ("r0c0",)


def test_derive_stats_best_objective_direction(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    fid = svc.commit_proposals(round_id=0, proposals=[ResearchProposal(
        instruction="x", research_target=NewFindingTarget(question="Q?"))])[0]
    # Same finding continued into round 1 -> two refs r0c0, r1c0.
    svc.commit_proposals(round_id=1, proposals=[ResearchProposal(
        instruction="y",
        research_target=ExistingFindingTarget(finding_id=fid))])
    # Two outcomes joined; lower_is_better -> best is min(110, 90) = 90.
    c1 = _cand(0, fid=fid, objective=90.0, sel=True)
    c1["experiment_id"] = "r1c0"
    _write_history(
        tmp_path,
        _round(0, _cand(0, fid=fid, objective=110.0, sel=True)),
        _round(1, c1),
    )
    derived = svc.inspect_finding(fid)["stats"]
    assert derived["attempts"] == 2
    assert derived["best_objective"] == 90.0  # lower_is_better -> min


def test_startup_pack_has_no_notebook_or_annotation_language(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    pack = svc.build_startup_pack(
        goal="fast", editable=["OMILREC/"], frozen=["tests/"],
        base_sha="abc", gate_block="- physics: pass",
        candidates_per_round=1, hints=None, current_round=0,
    )
    assert "notebook" not in pack.lower()
    assert "annotation" not in pack.lower()
    assert "ref: note" not in pack.lower()
    # It DOES advertise memory tools.
    assert "list_findings" in pack


def test_startup_pack_surfaces_recent_abstentions(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    _write_history(
        tmp_path,
        {
            "round": 0,
            "parent_sha": "p",
            "candidates": [],
            "abstention": {
                "reason": "No mechanism had direct evidence.",
                "blocking_unknown": "whether QPDF is still hot",
            },
        },
    )
    pack = svc.build_startup_pack(
        goal="fast", editable=["src/"], frozen=["tests/"],
        base_sha="abc", gate_block="- physics: pass",
        candidates_per_round=1, hints=None, current_round=1,
    )
    assert "abstention" in pack.lower()
    assert "No mechanism had direct evidence." in pack
    assert "whether QPDF is still hot" in pack


def test_startup_pack_omits_abstention_block_when_none(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    _write_history(tmp_path, _round(0, _cand(0)))
    pack = svc.build_startup_pack(
        goal="fast", editable=["src/"], frozen=["tests/"],
        base_sha="abc", gate_block="- physics: pass",
        candidates_per_round=1, hints=None, current_round=1,
    )
    assert "abstention" not in pack.lower()


def test_search_experiments_returns_buckets(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    _write_history(
        tmp_path,
        _round(0,
               _cand(0, fid="F-001", gate=True,
                     proposal="hoist QPDF cache lookup",
                     paths=("OMILREC/src/QPDF.cc",)),
               _cand(1, fid="F-001", gate=False,
                     proposal="hoist QPDF cache other way",
                     paths=("OMILREC/src/QPDF.cc",))),
    )
    result = svc.search_experiments(
        query="QPDF cache", filters=None, limit=6, buckets=True,
    )
    assert set(result) == {"relevant", "contrasting", "diverse"}
    assert any(h["experiment_id"] == "r0c0" for h in result["relevant"])


def test_list_findings_effective_state_reflects_dormancy(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS, dormancy_rounds=1)
    fid = svc.commit_proposals(
        round_id=0,
        proposals=[ResearchProposal(
            instruction="x",
            research_target=NewFindingTarget(question="Q"),
        )],
    )[0]
    # commit_proposals set it active (last_touched_round=0).
    active = svc.list_findings(state="active", current_round=1)
    assert active and active[0]["id"] == fid
    dormant = svc.list_findings(state="dormant", current_round=5)
    assert dormant and dormant[0]["id"] == fid
