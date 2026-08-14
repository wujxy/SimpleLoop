from __future__ import annotations

import json
from pathlib import Path

from simpleloop.stages.agent import Agent, _decode_output
from simpleloop.world import ProcessResult


def test_decode_output_extracts_structured_result_and_usage():
    result = _decode_output(json.dumps({
        "result": "fallback",
        "structured_output": {"proposals": ["try A"]},
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
        },
    }))

    assert result.text == "fallback"
    assert result.data == {"proposals": ["try A"]}
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
    }


def test_agent_notifies_usage_observer():
    seen = []
    agent = Agent(world=object(), usage_observer=seen.append)

    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "proposer")

    assert seen == [{"input_tokens": 1, "output_tokens": 2}]


def test_usage_observer_failure_is_nonfatal(capsys):
    def fail(_usage):
        raise OSError("state unavailable")

    agent = Agent(world=object(), usage_observer=fail)

    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "researcher")

    assert "[telemetry] warning:" in capsys.readouterr().out


def test_agent_runs_claude_through_world(tmp_path: Path):
    class FakeWorld:
        def __init__(self):
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            return ProcessResult(
                request.argv, 0, '{"result":"ok","usage":{}}', "", 0.1,
            )

    world = FakeWorld()
    agent = Agent(world=world, timeout_seconds=60)

    assert agent.run_text("prompt", cwd=tmp_path, label="executor") == "ok"
    assert world.requests[0].argv[0] == "claude"
    assert world.requests[0].stdin == "prompt"
