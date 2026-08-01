from __future__ import annotations

from types import SimpleNamespace

import pytest

from simpleloop.roles.model import HepAIChatModel, ModelError


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        message = SimpleNamespace(
            content='{"action":"submit_proposals","proposals":["p"]}'
        )
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 17})
        return SimpleNamespace(choices=[choice], usage=usage)


def test_hepai_uses_nonstreaming_chat_completion_and_timeout():
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = HepAIChatModel(client=client, model="gpt-5.5")

    reply = model.complete(
        system="scientist",
        messages=[{"role": "user", "content": "investigate"}],
        timeout_seconds=12.5,
    )

    assert completions.kwargs == {
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": "scientist"},
            {"role": "user", "content": "investigate"},
        ],
        "stream": False,
        "timeout": 12.5,
    }
    assert reply.text == (
        '{"action":"submit_proposals","proposals":["p"]}'
    )
    assert reply.usage == {"total_tokens": 17}


def test_hepai_requires_key_when_constructing_real_client(monkeypatch):
    monkeypatch.delenv("HEPAI_API_KEY", raising=False)
    with pytest.raises(ModelError, match="HEPAI_API_KEY"):
        HepAIChatModel.from_config({
            "model": "gpt-5.5",
            "base_url": "https://aiapi.ihep.ac.cn/apiv2",
        })


def test_hepai_rejects_empty_response():
    completions = _Completions()
    completions.create = lambda **_kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  "))],
        usage=None,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = HepAIChatModel(client=client, model="gpt-5.5")

    with pytest.raises(ModelError, match="empty"):
        model.complete(
            system="scientist", messages=[], timeout_seconds=1,
        )
