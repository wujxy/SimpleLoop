from __future__ import annotations

import json

from simpleloop.agent import Agent, _decode_output


def test_decode_output_extracts_structured_result_and_usage():
    result = _decode_output(json.dumps({
        "result": "fallback",
        "structured_output": {"score": 0.8},
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
        },
    }))

    assert result.text == "fallback"
    assert result.data == {"score": 0.8}
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
    }


def test_decode_output_keeps_legacy_plain_json_and_missing_usage():
    result = _decode_output('{"score": 0.8}')

    assert result.text == '{"score": 0.8}'
    assert result.data == {}
    assert result.usage is None


def test_agent_notifies_usage_observer():
    seen = []
    agent = Agent(usage_observer=seen.append)

    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "proposer")

    assert seen == [{"input_tokens": 1, "output_tokens": 2}]


def test_usage_observer_failure_is_nonfatal(capsys):
    def fail(_usage):
        raise OSError("state unavailable")

    agent = Agent(usage_observer=fail)

    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "judger")

    assert "[telemetry] warning:" in capsys.readouterr().out
