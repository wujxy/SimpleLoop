"""Tests for MemoryService target resolution + experiment linking + tools."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.memory import MemoryService
from simpleloop.memory.models import (
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


def test_link_completed_experiments_updates_finding(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    proposals = [
        ResearchProposal(
            instruction="First look.",
            research_target=NewFindingTarget(question="Q?"),
        ),
    ]
    fid = svc.resolve_targets(proposals, round_id=0)[0]
    _write_history(tmp_path, _round(0, _cand(0, fid=fid, objective=95.0)))
    svc.link_completed_experiments(round_id=0, candidates=[
        _cand(0, fid=fid, objective=95.0),
    ])
    updated = svc.finding_store.load_all()[fid]
    assert updated.state == "active"
    assert updated.experiment_refs == ("r0c0",)
    assert updated.stats["attempts"] == 1
    assert updated.stats["eligible"] == 1
    assert updated.stats["best_objective"] == 95.0
    assert updated.last_touched_round == 0


def test_link_updates_best_objective_direction(tmp_path: Path):
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    fid = svc.resolve_targets(
        [ResearchProposal(
            instruction="x",
            research_target=NewFindingTarget(question="Q?"),
        )],
        round_id=0,
    )[0]
    svc.link_completed_experiments(round_id=0, candidates=[
        _cand(0, fid=fid, objective=110.0),
    ])
    svc.link_completed_experiments(round_id=1, candidates=[
        _cand(0, fid=fid, objective=90.0),
    ])
    updated = svc.finding_store.load_all()[fid]
    # lower_is_better=True: best is min.
    assert updated.stats["best_objective"] == 90.0
    assert updated.last_touched_round == 1
    assert updated.experiment_refs == ("r0c0",)  # dedup on repeated id


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


def test_startup_pack_surfaces_deliberation_signals(tmp_path: Path):
    """A finding with repeated eligible-neutral attempts surfaces as a
    mechanism_challenge policy signal in the startup pack (facts and policy
    kept distinct, never framed as a scientific conclusion)."""
    svc = MemoryService(tmp_path, metrics_schema=METRICS)
    fid = svc.resolve_targets(
        [ResearchProposal(
            instruction="x",
            research_target=NewFindingTarget(question="Is QPDF the cost?"),
        )],
        round_id=0,
    )[0]

    def _chain(round_id, sha, parent, objective, sel=False):
        return {
            "round": round_id, "parent_sha": parent,
            "candidates": [{
                "candidate": 0, "experiment_id": f"r{round_id}c0",
                "finding_id": fid, "proposal": "p", "parent_sha": parent,
                "sha": sha, "status": "COMPLETED",
                "metrics": {"SPEED_MS": objective}, "changed_paths": ["src/a.cc"],
                "gates": {}, "gate_passed": True, "eligible": True,
                "selected": sel,
            }],
        }

    _write_history(
        tmp_path,
        _chain(0, "s0", "root", 90.0, sel=True),
        _chain(1, "s1", "s0", 90.0),   # neutral vs s0
        _chain(2, "s2", "s1", 90.0),   # neutral vs s1
    )
    pack = svc.build_startup_pack(
        goal="fast", editable=["src/"], frozen=["tests/"],
        base_sha="abc", gate_block="- physics: pass",
        candidates_per_round=1, hints=None, current_round=3,
    )
    assert "Explore health" in pack
    assert "mechanism_challenge" in pack
    assert "NOT" in pack and "verdicts" in pack  # the non-verdict disclaimer


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
    fid = svc.resolve_targets(
        [ResearchProposal(
            instruction="x",
            research_target=NewFindingTarget(question="Q"),
        )],
        round_id=0,
    )[0]
    # Set it active
    svc.link_completed_experiments(round_id=0, candidates=[
        _cand(0, fid=fid, objective=100.0),
    ])
    active = svc.list_findings(state="active", current_round=1)
    assert active and active[0]["id"] == fid
    dormant = svc.list_findings(state="dormant", current_round=5)
    assert dormant and dormant[0]["id"] == fid
