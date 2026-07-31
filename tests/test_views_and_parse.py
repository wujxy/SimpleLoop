"""Unit-level assertions for the role-boundary + landing-state refactor.

These do NOT spawn the claude agent — they test the pure pieces:
  - views.for_proposer projects sha/metrics/changed_paths (landing-state signals)
    while still excluding eval_block
  - judger._parse handles the four-field contract + the missing-report fallback
    (LANDED_STATE prefix is a feedback-string convention, not a parsed field)
  - Store.append persists both feedback fields + changed_paths

Run: python -m pytest tests/   (from SimpleLoop/)
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop.harness import views
from simpleloop.roles.agent import normalize_free_text
from simpleloop.roles.judger import Judgment, _parse, _build_prompt, _judger_schema, judge
from simpleloop.harness import store as store_mod
from simpleloop.harness.store import Store

def test_normalize_free_text_accepts_limit_without_warning(capsys):
    value = "x" * 1100

    assert normalize_free_text(
        value, limit=1100, label="proposer", field="reflection"
    ) == value
    assert capsys.readouterr().out == ""


def test_normalize_free_text_warns_and_truncates_after_strip(capsys):
    value = "  " + ("x" * 1101) + "  "

    assert normalize_free_text(
        value, limit=1100, label="proposer", field="reflection"
    ) == "x" * 1100
    assert capsys.readouterr().out == (
        "[proposer] warning: reflection length 1101 exceeds 1100; "
        "truncated to 1100\n"
    )


# --- views.for_proposer: projects landing-state signals to the proposer ---

def test_for_proposer_projects_only_factual_candidate_state():
    history = [{
        "round": 0,
        "parent_sha": "base",
        "selected_candidate": 0,
        "selected_sha": "candidate",
        "base_sha": "candidate",
        "candidates": [{
            "candidate": 0,
            "proposal": "rewrite kernel",
            "parent_sha": "base",
            "sha": "candidate",
            "status": "COMPLETED",
            "selected": True,
            "gate_passed": True,
            "eligible": True,
            "gates": {"PATHS": {"passed": True, "detail": ""}},
            "metrics": {"SPEED_MS": 90.0},
            "changed_paths": ["src/kernel.cc"],
            "eval_block": "raw evaluator output",
            "feedback": "legacy narrative",
            "score": 0.9,
        }],
    }]

    projected = views.for_proposer(history)

    assert projected[0]["candidates"] == [{
        "candidate": 0,
        "proposal": "rewrite kernel",
        "parent_sha": "base",
        "sha": "candidate",
        "status": "COMPLETED",
        "selected": True,
        "gate_passed": True,
        "eligible": True,
        "gates": {"PATHS": {"passed": True, "detail": ""}},
        "metrics": {"SPEED_MS": 90.0},
        "changed_paths": ["src/kernel.cc"],
    }]

def test_for_proposer_empty_history():
    assert views.for_proposer([]) == []


def test_for_proposer_preserves_order():
    history = [_serial_history_record(r, f"p{r}") for r in range(5)]
    out = views.for_proposer(history)
    assert [row["round"] for row in out] == [0, 1, 2, 3, 4]


def _serial_history_record(round_id: int, proposal: str) -> dict:
    """A one-candidate generation record (the shape append_generation writes)."""
    return {
        "round": round_id,
        "parent_sha": f"parent-{round_id}",
        "selected_candidate": 0,
        "selected_sha": f"sha-{round_id}",
        "base_sha": f"sha-{round_id}",
        "reflection": "reflection",
        "candidates": [
            {
                "candidate": 0,
                "family": "single",
                "proposal": proposal,
                "sha": f"sha-{round_id}",
                "selected": True,
                "accepted": True,
                "score": 0.5,
                "risk": "low",
                "metrics": {"SPEED_MS": 500.0 + round_id},
                "changed_paths": ["src/a.cc"],
                "feedback": f"LANDED_STATE: not-implemented\nImplemented: x{round_id}\nResult: y{round_id}\nAnalysis: z{round_id}",
                "feedback_for_proposer": f"short lesson {round_id}",
                "eval_block": "raw output",
            }
        ],
    }


def _parallel_history_record(round_id: int, proposals: list[str]) -> dict:
    return {
        "round": round_id,
        "parent_sha": f"parent-{round_id}",
        "selected_candidate": 1,
        "selected_sha": f"candidate-{round_id}-1",
        "base_sha": f"candidate-{round_id}-1",
        "reflection": "reflection",
        "candidates": [
            {
                "candidate": candidate_id,
                "family": f"family-{candidate_id}",
                "proposal": proposal,
                "sha": f"candidate-{round_id}-{candidate_id}",
                "selected": candidate_id == 1,
                "accepted": True,
                "score": 0.4 + candidate_id / 10,
                "risk": "low",
                "metrics": {"SPEED_MS": 600.0 - candidate_id},
                "changed_paths": [f"src/c{candidate_id}.cc"],
                "feedback": f"LANDED_STATE: not-implemented\nImplemented: x{candidate_id}\nResult: y{candidate_id}\nAnalysis: z{candidate_id}",
                "feedback_for_proposer": f"short candidate lesson {candidate_id}",
                "eval_block": "raw output",
            }
            for candidate_id, proposal in enumerate(proposals)
        ],
    }


def test_for_proposer_recent_window_uses_position_and_full_proposals():
    round_ids = [3, 5, 12, 20, 21, 40, 99]
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in round_ids
    ]

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == round_ids[1:]
    assert [row["candidates"][0]["proposal"] for row in projected] == [
        f"proposal-{round_id}" for round_id in round_ids[1:]
    ]
    assert all(
        "proposal_head" not in c for row in projected for c in row["candidates"]
    )


def test_for_proposer_recent_window_does_not_mutate_history():
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in range(7)
    ]
    original = deepcopy(history)

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == [1, 2, 3, 4, 5, 6]
    assert history == original


def test_for_proposer_drops_old_generation_and_keeps_recent_candidates():
    old_generation = _parallel_history_record(
        10, ["old candidate zero", "old candidate one"]
    )
    recent_generation = _parallel_history_record(
        100, ["recent candidate zero", "recent candidate one"]
    )
    history = [old_generation]
    history.extend(
        _serial_history_record(round_id, f"recent-{round_id}")
        for round_id in [20, 30, 40, 50, 60]
    )
    history.append(recent_generation)

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == [20, 30, 40, 50, 60, 100]
    recent_candidates = projected[-1]["candidates"]
    assert [c["proposal"] for c in recent_candidates] == [
        "recent candidate zero",
        "recent candidate one",
    ]
    assert all("proposal_head" not in c for c in recent_candidates)
    assert all("eval_block" not in c for c in recent_candidates)


# --- judger._parse: four-field contract + fallback (risk now required) ---

def test_parse_preserves_feedback_for_proposer():
    jd = _parse({
        "score": 0.5,
        "risk": "low",
        "feedback": "LANDED_STATE: not-implemented\nImplemented: x\nResult: y\nAnalysis: z",
        "feedback_for_proposer": "The mechanism remains inconclusive.",
    })
    assert jd.feedback_for_proposer == "The mechanism remains inconclusive."


@pytest.mark.parametrize("value", [None, "", "   ", 7])
def test_parse_rejects_invalid_feedback_for_proposer(value):
    with pytest.raises(ValueError):
        _parse({
            "score": 0.5,
            "risk": "low",
            "feedback": "full",
            "feedback_for_proposer": value,
        })


def test_judger_schema_requires_feedback_for_proposer():
    schema = _judger_schema()
    assert schema["required"] == [
        "score", "risk", "feedback", "feedback_for_proposer",
    ]
    assert schema["properties"]["feedback_for_proposer"]["pattern"] == r"\S"


def test_parse_four_field_contract():
    jd = _parse({"score": 0.83, "risk": "low",
                 "feedback": "LANDED_STATE: not-implemented\nImplemented: precompute sqrt\nResult: 843ms vs 945ms -11%\nAnalysis: cache locality improvement",
                 "feedback_for_proposer": "Precomputation remains promising."})
    assert isinstance(jd, Judgment)
    assert jd.score == 0.83
    assert jd.risk == "low"
    assert "LANDED_STATE" in jd.feedback


def test_parse_rejects_missing_risk():
    """risk is required for best selection (high-risk rounds never ship).
    Older judger output without risk must fail loudly, not silently default."""
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "feedback": "short",
                "feedback_for_proposer": "short lesson"})


def test_parse_rejects_bad_risk():
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "maybe", "feedback": "x",
                "feedback_for_proposer": "short lesson"})
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "", "feedback": "x",
                "feedback_for_proposer": "short lesson"})


def test_parse_rejects_bad_score():
    with pytest.raises(ValueError):
        _parse({"score": 1.5, "risk": "low", "feedback": "x",
                "feedback_for_proposer": "short lesson"})
    with pytest.raises(ValueError):
        _parse({"score": "nan", "risk": "low", "feedback": "x",
                "feedback_for_proposer": "short lesson"})


@pytest.mark.parametrize(
    "data",
    [
        {"score": 0.5, "risk": "low", "feedback": "x",
         "feedback_for_proposer": "short lesson", "extra": 1},
        {"score": "0.5", "risk": "low", "feedback": "x",
         "feedback_for_proposer": "short lesson"},
        {"score": True, "risk": "low", "feedback": "x",
         "feedback_for_proposer": "short lesson"},
        {"score": 0.5, "risk": 1, "feedback": "x",
         "feedback_for_proposer": "short lesson"},
    ],
)
def test_parse_rejects_values_outside_exact_schema_contract(data):
    with pytest.raises(ValueError):
        _parse(data)


def test_parse_rejects_empty_feedback():
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "low", "feedback": "   ",
                "feedback_for_proposer": "short lesson"})


def test_judger_schema_keeps_structure_without_text_max_lengths():
    schema = _judger_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "score", "risk", "feedback", "feedback_for_proposer",
    ]
    assert schema["properties"]["score"] == {
        "type": "number", "minimum": 0.0, "maximum": 1.0,
    }
    assert schema["properties"]["risk"]["enum"] == ["low", "medium", "high"]
    assert "maxLength" not in schema["properties"]["feedback"]
    assert schema["properties"]["feedback"]["pattern"] == r"\S"
    assert "maxLength" not in schema["properties"]["feedback_for_proposer"]
    assert schema["properties"]["feedback_for_proposer"]["pattern"] == r"\S"


def test_judge_passes_schema_and_custom_label(tmp_path: Path, capsys):
    class CapturingAgent:
        schema = None
        label = None
        prompt = None

        def run_json(self, prompt, *, json_schema=None, label=None, **_kwargs):
            self.prompt = prompt
            self.schema = json_schema
            self.label = label
            return {"score": 0.5, "risk": "low", "feedback": "x" * 1001,
                    "feedback_for_proposer": "short lesson"}

    agent = CapturingAgent()
    judgment = judge(
        agent,
        goal="g",
        proposal="p",
        sha=None,
        reason="no change",
        parent_sha="base",
        workspace=None,
        eval_block="",
        cwd=tmp_path,
        label="judger r1-c0",
    )

    assert judgment.feedback == "x" * 1000
    assert agent.schema == _judger_schema()
    assert "LANDED_STATE:" in agent.prompt
    assert "LANDING_STATE:" not in agent.prompt
    assert "`feedback_for_proposer`" in agent.prompt
    assert agent.label == "judger r1-c0"
    assert capsys.readouterr().out == (
        "[judger r1-c0] warning: feedback length 1001 exceeds 1000; "
        "truncated to 1000\n"
    )


def test_parse_accepts_judger_n_plus_500_without_warning(capsys):
    judgment = _parse(
        {
            "score": 0.5,
            "risk": "low",
            "feedback": "f" * 1000,
            "feedback_for_proposer": "p" * 800,
        },
        label="judger r1-c0",
    )

    assert len(judgment.feedback) == 1000
    assert len(judgment.feedback_for_proposer) == 800
    assert capsys.readouterr().out == ""


def test_parse_warns_and_truncates_judger_text_with_candidate_label(capsys):
    judgment = _parse(
        {
            "score": 0.5,
            "risk": "low",
            "feedback": "f" * 1001,
            "feedback_for_proposer": "p" * 801,
        },
        label="judger r14-c2",
    )

    assert len(judgment.feedback) == 1000
    assert len(judgment.feedback_for_proposer) == 800
    assert capsys.readouterr().out == (
        "[judger r14-c2] warning: feedback length 1001 exceeds 1000; "
        "truncated to 1000\n"
        "[judger r14-c2] warning: feedback_for_proposer length 801 exceeds 800; "
        "truncated to 800\n"
    )



# --- Store: feedback fields remain persisted in history ---

_STORE_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
                 "gates": [{"key": "CORRECTNESS"}]}


def test_store_persists_only_factual_candidate_fields(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    candidate = {
        "candidate": 0,
        "proposal": "replace the reconstruction kernel",
        "parent_sha": "parent",
        "sha": "candidate",
        "status": "COMPLETED",
        "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": True},
        "changed_paths": ["src/new_kernel.cc"],
        "gates": {
            "PATHS": {"passed": True, "detail": ""},
            "EVAL_COMMANDS": {"passed": True, "detail": ""},
            "CORRECTNESS": {"passed": True, "detail": ""},
        },
        "gate_passed": True,
        "eligible": True,
        "selected": False,
    }

    store.append_generation(
        0,
        parent_sha="parent",
        selected_candidate=0,
        selected_sha="candidate",
        candidates=[candidate],
    )

    row = store.history()[0]
    assert row["candidates"][0]["status"] == "COMPLETED"
    assert row["candidates"][0]["gates"]["PATHS"]["passed"] is True
    assert not ({"score", "risk", "feedback", "feedback_for_proposer",
                 "family", "decision", "accepted"}
                & row["candidates"][0].keys())


def test_legacy_high_risk_candidate_stays_ineligible():
    legacy = {
        "sha": "old",
        "risk": "high",
        "metrics": {"SPEED_MS": 80.0, "CORRECTNESS": True},
    }

    assert store_mod.eligible(legacy, _STORE_SCHEMA) is False


def test_new_candidate_cannot_override_failed_gate_with_eligible_flag():
    inconsistent = {
        "sha": "candidate",
        "gate_passed": False,
        "eligible": True,
        "metrics": {"SPEED_MS": 80.0, "CORRECTNESS": False},
    }

    assert store_mod.eligible(inconsistent, _STORE_SCHEMA) is False


# --- Store: changed_paths persisted (landing-state signal for the proposer) ---

def test_store_persists_changed_paths(tmp_path: Path):
    """append_generation stores changed_paths so the proposer can see what each
    round touched without running git."""
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    store.append_generation(
        0, parent_sha="parent", selected_candidate=0, selected_sha="sha0",
        candidates=[{
            "candidate": 0, "proposal": "p0", "sha": "sha0", "score": 0.7,
            "risk": "low", "accepted": True, "feedback": "f",
            "changed_paths": ["a.cc", "a.h"],
        }])
    rows = store.history()
    assert rows[0]["changed_paths"] == ["a.cc", "a.h"]
    assert rows[0]["candidates"][0]["changed_paths"] == ["a.cc", "a.h"]


def test_store_persists_candidate_acceptance_and_resulting_base(tmp_path: Path):
    """A rejected implementation keeps its candidate SHA while the accepted
    base stays on the prior commit."""
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    store.append_generation(
        0, parent_sha="baseline", selected_candidate=None, selected_sha=None,
        candidates=[{
            "candidate": 0, "proposal": "p0", "sha": "candidate0", "score": 0.1,
            "risk": "low", "accepted": False, "feedback": "correctness failed",
            "metrics": {"CORRECTNESS": False}, "changed_paths": ["a.cc"],
        }])
    row = store.history()[0]
    assert row["candidates"][0]["sha"] == "candidate0"
    assert row["candidates"][0]["gate_passed"] is False
    assert row["candidates"][0]["eligible"] is False
    assert row["selected_sha"] is None
    assert row["base_sha"] == "baseline"


def test_store_changed_paths_default_empty(tmp_path: Path):
    """Candidates without changed_paths (gate-rejected / failed rounds) read
    back as an empty list, not a missing key — the proposer can rely on the
    field always being present."""
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    store.append_generation(
        0, parent_sha="parent", selected_candidate=None, selected_sha=None,
        candidates=[{
            "candidate": 0, "proposal": "p0", "sha": "sha0", "score": 0.0,
            "risk": "high", "accepted": False,
            "feedback": "[loop failure] boom",
        }])
    rows = store.history()
    assert rows[0]["candidates"][0]["changed_paths"] == []


# --- judger: LANDED_STATE prefix is a feedback convention, not a parsed field ---

def test_parse_accepts_landed_state_prefixed_feedback():
    """feedback with the LANDED_STATE: prefix (the new contract) parses the same
    as any other feedback string — the prefix is a convention the proposer reads,
    not a field _parse extracts."""
    jd = _parse({"score": 0.05, "risk": "low",
                 "feedback": "LANDED_STATE: already-implemented empty diff, 459.3ms",
                 "feedback_for_proposer": "The mechanism was already present."})
    assert jd.score == 0.05
    assert jd.risk == "low"
    assert jd.feedback == "LANDED_STATE: already-implemented empty diff, 459.3ms"


def test_resume_chain_skips_rejected_tail_and_uses_last_accepted_metrics():
    from simpleloop import loop as loop_mod

    history = [
        {"round": 0, "selected_candidate": 0, "selected_sha": "good",
         "candidates": [{"candidate": 0, "sha": "good", "selected": True,
                         "metrics": {"CORRECTNESS": True, "SPEED_MS": 100.0}}]},
        {"round": 1, "selected_candidate": None, "selected_sha": None,
         "candidates": [{"candidate": 0, "sha": "bad", "selected": False,
                         "metrics": {"CORRECTNESS": False}}]},
    ]
    sha, record = loop_mod._resume_chain(history, "baseline")
    assert sha == "good"
    assert record is history[0]["candidates"][0]
    assert record["metrics"]["SPEED_MS"] == 100.0


def test_resume_chain_falls_back_to_baseline_when_no_candidate_was_accepted():
    from simpleloop import loop as loop_mod

    history = [
        {"round": 0, "selected_candidate": None, "selected_sha": None,
         "candidates": [{"candidate": 0, "sha": "bad", "selected": False,
                         "metrics": {"CORRECTNESS": False}}]},
    ]
    assert loop_mod._resume_chain(history, "baseline") == ("baseline", None)


def test_gate_block_renders_key_and_description_lines():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [
            {"key": "FCN", "description": "likelihood drift at 1e-13"},
            {"key": "CONSISTENCY", "description": "physics within tolerance"},
        ],
    }
    block = views.gate_block(schema)
    assert "- FCN: likelihood drift at 1e-13" in block
    assert "- CONSISTENCY: physics within tolerance" in block
    # header/framing prose lives in the prompt, not here:
    assert "Gates" not in block


def test_gate_block_empty_when_no_description():
    # gates without a description produce nothing (degrades to today's prompt):
    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
              "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}]}
    assert views.gate_block(schema) == ""


def test_gate_block_empty_when_no_schema():
    assert views.gate_block(None) == ""


def _write_min_task(tmp_path: Path, loop: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"fake-sif")
    raw = {
        "kind": "task",
        "task": {"goal": "test"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": loop,
        "source": {"path": str(repo)},
        "runtime": {"image": str(image)},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [{"key": "CORRECTNESS"}],
            },
        },
    }
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_config_proposer_recent_rounds_defaults_to_three(tmp_path: Path):
    cfg = config_mod.load(_write_min_task(tmp_path, {"max_rounds": 1}))
    assert cfg["proposer_recent_rounds"] == 6


def test_config_proposer_recent_rounds_is_configurable(tmp_path: Path):
    cfg = config_mod.load(
        _write_min_task(tmp_path, {"max_rounds": 1, "proposer_recent_rounds": 5})
    )
    assert cfg["proposer_recent_rounds"] == 5


def test_config_proposer_recent_rounds_rejects_non_positive(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="proposer_recent_rounds"):
        config_mod.load(
            _write_min_task(tmp_path, {"max_rounds": 1, "proposer_recent_rounds": 0})
        )
