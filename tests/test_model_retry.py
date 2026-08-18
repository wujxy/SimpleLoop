"""Tests for the transient-HTTP retry in OpenAICompatChatModel.

A flaky upstream gateway (504/503/502) or a momentary connection drop must not
burn a Scientist round: the adapter retries transient failures with backoff and
re-raises only after exhausting attempts. Non-transient errors (400/401/403)
are not retried — they won't fix themselves. Replies arrive as SSE chunk
streams (see the adapter docstring for why), and a stream that dies MID-reply
is retried exactly like a failed request.
"""
from __future__ import annotations

import pytest

from proposer import model as model_mod
from proposer.model import ModelError, OpenAICompatChatModel, _is_transient


# --- fakes -----------------------------------------------------------------

class _FakeStatusError(Exception):
    """Mimics openai.APIStatusError / HAPIStatusError: carries status_code."""
    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status


class _APITimeoutError(Exception):
    """Connection/timeout class — name contains 'timeout' so _is_transient
    catches it via the MRO check (no status_code attr)."""


class _FakeUsage:
    def model_dump(self) -> dict:
        return {"prompt_tokens": 10, "completion_tokens": 5}


class _FakeDelta:
    def __init__(self, content: str | None, reasoning_content: str | None = None):
        self.content = content
        if reasoning_content is not None:
            self.reasoning_content = reasoning_content


class _FakeChunkChoice:
    def __init__(self, delta: _FakeDelta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, content: str | None = None, *, reasoning=None,
                 usage=None):
        self.choices = [_FakeChunkChoice(_FakeDelta(content, reasoning))] \
            if content is not None or reasoning is not None else []
        if usage is not None:
            self.usage = usage


class _FakeStream:
    """Iterable of chunks; an Exception member is raised at that point."""
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        for item in self._chunks:
            if isinstance(item, Exception):
                raise item
            yield item


class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        self._owner.calls += 1
        self._owner.last_kwargs = kwargs
        outcome = self._owner._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeChat:
    def __init__(self, owner):
        self.completions = _FakeCompletions(owner)


class _FakeClient:
    """``client.chat.completions.create`` pops scripted outcomes in order.

    Each outcome is either an Exception (raised) or a list of stream chunks
    (returned as _FakeStream).
    """
    def __init__(self, outcomes):
        self._outcomes = [
            o if isinstance(o, Exception) else _FakeStream(o)
            for o in outcomes
        ]
        self.calls = 0
        self.last_kwargs = None

    @property
    def chat(self):
        return _FakeChat(self)


def _model(outcomes, *, max_retries=3, retry_base_delay=0.01):
    client = _FakeClient(outcomes)
    m = OpenAICompatChatModel(
        client=client, model="m",
        max_retries=max_retries, retry_base_delay=retry_base_delay,
    )
    m._fake_client = client
    return m


def _ok_chunks(text: str, with_usage: bool = True):
    chunks = [_FakeChunk(c) for c in text]
    if with_usage:
        chunks.append(_FakeChunk(usage=_FakeUsage()))
    return chunks


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Retry backoff must never actually sleep in tests."""
    monkeypatch.setattr(model_mod.time, "sleep", lambda _s: None)


# --- _is_transient ---------------------------------------------------------

def test_is_transient_by_status_code():
    for code in (408, 429, 500, 502, 503, 504):
        assert _is_transient(_FakeStatusError(code)) is True


def test_is_not_transient_by_status_code():
    for code in (400, 401, 403, 404, 422):
        assert _is_transient(_FakeStatusError(code)) is False


def test_is_transient_timeout_by_class_name():
    assert _is_transient(_APITimeoutError()) is True


def test_is_transient_by_gateway_message():
    class Plain(Exception):
        pass
    assert _is_transient(Plain("504 Gateway Time-out")) is True
    assert _is_transient(Plain("service unavailable")) is True


def test_is_transient_empty_reply():
    assert _is_transient(model_mod.EmptyReplyError("empty")) is True


def test_is_not_transient_generic_error():
    class ParseError(Exception):
        pass
    assert _is_transient(ParseError("invalid json")) is False


# --- complete() retry behavior --------------------------------------------

def test_retry_succeeds_after_one_transient():
    model = _model([_FakeStatusError(504), _ok_chunks("ok")])
    reply = model.complete(system="s", messages=[{"role": "user", "content": "hi"}],
                           timeout_seconds=60)
    assert reply.text == "ok"
    assert model.client.calls == 2  # one failure + one success


def test_retry_succeeds_after_timeout_then_503():
    model = _model([
        _APITimeoutError(), _FakeStatusError(503), _ok_chunks("recovered"),
    ])
    reply = model.complete(system="s", messages=[], timeout_seconds=60)
    assert reply.text == "recovered"
    assert model.client.calls == 3


def test_retry_exhausts_then_raises():
    model = _model([_FakeStatusError(504)] * 10, max_retries=3)
    with pytest.raises(_FakeStatusError):
        model.complete(system="s", messages=[], timeout_seconds=60)
    assert model.client.calls == 4  # initial + 3 retries


def test_no_retry_on_non_transient():
    # A 400 is permanent, so no transient RETRY — but the stream_options
    # fallback probe (one immediate resend without the flag, for gateways
    # that reject it) fires first; the second 400 then raises. calls==2 is
    # the probe, not a retry loop.
    model = _model([_FakeStatusError(400), _FakeStatusError(400)])
    with pytest.raises(_FakeStatusError):
        model.complete(system="s", messages=[], timeout_seconds=60)
    assert model.client.calls == 2
    assert "stream_options" not in model.client.last_kwargs


def test_no_retry_on_generic_error():
    class ParseError(Exception):
        pass
    model = _model([ParseError("bad")])
    with pytest.raises(ParseError):
        model.complete(system="s", messages=[], timeout_seconds=60)
    assert model.client.calls == 1


def test_empty_reply_retried_then_raises():
    # an empty reply is usually self-healing for streaming reasoning models
    # (thinking can exhaust the output budget; gateways can truncate the
    # stream), so it retries like any transient failure and only raises
    # after exhausting attempts.
    model = _model([[], [], [], []], max_retries=3)
    with pytest.raises(ModelError, match="empty"):
        model.complete(system="s", messages=[], timeout_seconds=60)
    assert model.client.calls == 4  # initial + 3 retries


def test_empty_reply_recovers_on_retry():
    model = _model([[], _ok_chunks("ok")])
    reply = model.complete(system="s", messages=[{"role": "user", "content": "hi"}],
                           timeout_seconds=60)
    assert reply.text == "ok"
    assert model.client.calls == 2


def test_empty_reply_error_carries_finish_reason():
    # finish_reason=length means thinking ate the output budget — the
    # diagnostic must reach the error message so the operator can tell it
    # apart from a truncated stream (finish_reason=None).
    chunks = [_FakeChunk(reasoning="thinking hard")]
    chunks[0].choices[0].finish_reason = "length"
    model = _model([chunks], max_retries=0)
    with pytest.raises(ModelError, match=r"finish_reason=length"):
        model.complete(system="s", messages=[], timeout_seconds=60)


def test_deadline_caps_retry_no_overshoot():
    # a near-zero deadline: after the first failure, no budget remains to sleep,
    # so the transient error re-raises immediately rather than overshooting.
    model = _model(
        [_FakeStatusError(504), _ok_chunks("ok")],
        max_retries=3, retry_base_delay=5.0,  # would sleep ~5s if allowed
    )
    with pytest.raises(_FakeStatusError):
        model.complete(system="s", messages=[], timeout_seconds=0.001)
    assert model.client.calls == 1  # never got to attempt 2


# --- streaming reply assembly ----------------------------------------------

def test_stream_content_concatenated_and_usage_captured():
    model = _model([_ok_chunks('{"action":"ok"}')])
    reply = model.complete(system="s", messages=[], timeout_seconds=60)
    assert reply.text == '{"action":"ok"}'
    assert reply.usage == {"prompt_tokens": 10, "completion_tokens": 5}


def test_reasoning_deltas_are_skipped():
    # reasoning arrives in delta.reasoning_content (GLM) or interleaved
    # chunks; only content deltas may reach the Scientist protocol.
    chunks = [
        _FakeChunk(reasoning="hmm, considering the gate first..."),
        _FakeChunk('{"action":'),
        _FakeChunk(reasoning="and now the command"),
        _FakeChunk('"run"}'),
        _FakeChunk(usage=_FakeUsage()),
    ]
    model = _model([chunks])
    reply = model.complete(system="s", messages=[], timeout_seconds=60)
    assert reply.text == '{"action":"run"}'


def test_mid_stream_death_is_retried():
    # a stream that yields chunks then dies (gateway cut mid-reply) must
    # retry the whole call, not surface the partial text.
    dying = [_FakeChunk('{"act'), _APITimeoutError()]
    model = _model([dying, _ok_chunks('{"action":"ok"}')])
    reply = model.complete(system="s", messages=[], timeout_seconds=60)
    assert reply.text == '{"action":"ok"}'
    assert model.client.calls == 2


def test_stream_options_rejected_falls_back_once():
    # some OpenAI-compatible gateways reject stream_options with a 400 —
    # the adapter drops the flag and retries immediately, then remembers.
    class _PickyCompletions:
        def __init__(self):
            self.calls = []
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if "stream_options" in kwargs:
                raise _FakeStatusError(400, "unknown parameter: stream_options")
            return _FakeStream(_ok_chunks("fine", with_usage=False))

    class _PickyClient:
        def __init__(self):
            self.chat = type("C", (), {"completions": _PickyCompletions()})()

    client = _PickyClient()
    model = OpenAICompatChatModel(
        client=client, model="m", max_retries=1, retry_base_delay=0.01)
    for _ in range(2):
        reply = model.complete(system="s", messages=[], timeout_seconds=60)
        assert reply.text == "fine"
    calls = client.chat.completions.calls
    assert len(calls) == 3  # 400 + retry, then a clean single call
    assert ["stream_options" in c for c in calls] == [True, False, False]
