from __future__ import annotations

from types import SimpleNamespace

import pytest

from proposer.model import (
    HepAIChatModel,
    ModelError,
    ZhipuChatModel,
    build_chat_model,
)


def _stream(text: str, usage=None):
    """A fake SSE chunk stream: one content delta per char, usage last."""
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=c))],
            usage=None,
        )
        for c in text
    ]
    if usage is not None:
        chunks.append(SimpleNamespace(choices=[], usage=usage))
    return iter(chunks)


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 17})
        return _stream(
            '{"action":"submit_proposals","proposals":["p"]}', usage)


def test_hepai_uses_streaming_chat_completion_and_timeout():
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = HepAIChatModel(client=client, model="gpt-5.5")

    reply = model.complete(
        system="scientist",
        messages=[{"role": "user", "content": "investigate"}],
        timeout_seconds=12.5,
    )

    # timeout is now the deadline-derived remaining budget (≤ the input, since
    # transient-retry eats into it), so it floats just below 12.5; check it
    # apart from the exact-equality kwargs.
    kwargs = dict(completions.kwargs)
    timeout = kwargs.pop("timeout")
    assert kwargs == {
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": "scientist"},
            {"role": "user", "content": "investigate"},
        ],
        "stream": True,
        "response_format": {"type": "json_object"},
        "stream_options": {"include_usage": True},
    }
    assert timeout == pytest.approx(12.5, abs=0.5)
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
    completions.create = lambda **_kwargs: _stream("  ")
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = HepAIChatModel(client=client, model="gpt-5.5")

    with pytest.raises(ModelError, match="empty"):
        model.complete(
            system="scientist", messages=[], timeout_seconds=1,
        )


def test_zhipu_uses_streaming_chat_completion_and_timeout():
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = ZhipuChatModel(client=client, model="glm-5.2")

    reply = model.complete(
        system="scientist",
        messages=[{"role": "user", "content": "investigate"}],
        timeout_seconds=12.5,
    )

    kwargs = dict(completions.kwargs)
    timeout = kwargs.pop("timeout")
    assert kwargs == {
        "model": "glm-5.2",
        "messages": [
            {"role": "system", "content": "scientist"},
            {"role": "user", "content": "investigate"},
        ],
        "stream": True,
        "response_format": {"type": "json_object"},
        "stream_options": {"include_usage": True},
    }
    assert timeout == pytest.approx(12.5, abs=0.5)
    assert reply.text == (
        '{"action":"submit_proposals","proposals":["p"]}'
    )
    assert reply.usage == {"total_tokens": 17}


def test_zhipu_requires_key_when_constructing_real_client(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    with pytest.raises(ModelError, match="ZHIPU_API_KEY"):
        ZhipuChatModel.from_config({
            "model": "glm-5.2",
            "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        })


def test_build_chat_model_dispatches_on_api(monkeypatch):
    # The OpenAI client constructor is lazy (no network at build time), so a
    # real ZhipuChatModel can be built here against a dummy key.
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    model = build_chat_model({
        "api": "zhipu",
        "model": "glm-5.2",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
    })
    assert isinstance(model, ZhipuChatModel)
    assert model.model == "glm-5.2"


def test_build_chat_model_routes_hepai_through_monkeypatchable_global():
    # Tests inject a fake via model_mod.HepAIChatModel; build_chat_model must
    # resolve that name at call time (module global), not bind it eagerly.
    class _FakeHepAI:
        @classmethod
        def from_config(cls, config):
            return cls()

    import proposer.model as model_mod
    monkeypatch_obj = _FakeHepAI
    original = model_mod.HepAIChatModel
    model_mod.HepAIChatModel = monkeypatch_obj
    try:
        built = build_chat_model({
            "api": "hepai",
            "model": "gpt-5.5",
            "base_url": "https://aiapi.ihep.ac.cn/apiv2",
        })
        assert isinstance(built, _FakeHepAI)
    finally:
        model_mod.HepAIChatModel = original


def test_build_chat_model_rejects_unknown_api():
    with pytest.raises(ModelError, match="unsupported provider"):
        build_chat_model({"api": "nope", "model": "x", "base_url": "u"})


# --- thinking-depth valve (config: roles.researcher.reasoning_effort) -----

def test_effort_default_absent_from_request():
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = HepAIChatModel(client=client, model="gpt-5.5")
    model.complete(system="s", messages=[], timeout_seconds=5)
    assert "reasoning_effort" not in completions.kwargs


def test_effort_passed_through_on_hepai():
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = HepAIChatModel(
        client=client, model="gpt-5.5", reasoning_effort="medium")
    model.complete(system="s", messages=[], timeout_seconds=5)
    assert completions.kwargs["reasoning_effort"] == "medium"


def test_effort_invalid_fails_fast():
    from proposer.model import _validated_effort
    with pytest.raises(ModelError, match="reasoning_effort"):
        _validated_effort({"reasoning_effort": "turbo"})
    assert _validated_effort({}) is None
    assert _validated_effort({"reasoning_effort": " Low "}) == "low"


def test_effort_translated_to_zhipu_thinking_switch(monkeypatch):
    # GLM has no graded knob: from_config translates low -> disabled,
    # medium/high -> enabled, carried via the SDK's extra_body; absent
    # config leaves no knob at all.
    monkeypatch.setenv("ZHIPU_API_KEY", "k")
    low = ZhipuChatModel.from_config({
        "model": "glm-5.3", "base_url": "https://x",
        "reasoning_effort": "low"})
    high = ZhipuChatModel.from_config({
        "model": "glm-5.3", "base_url": "https://x",
        "reasoning_effort": "high"})
    off = ZhipuChatModel.from_config({
        "model": "glm-5.3", "base_url": "https://x"})
    assert low.extra_body == {"thinking": {"type": "disabled"}}
    assert high.extra_body == {"thinking": {"type": "enabled"}}
    assert not getattr(off, "extra_body", None)
    assert low.reasoning_effort is None  # never sent as a raw param to GLM
