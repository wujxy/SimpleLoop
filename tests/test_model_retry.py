"""Tests for the transient-HTTP retry in OpenAICompatChatModel.

A flaky upstream gateway (504/503/502) or a momentary connection drop must not
burn a Scientist round: the adapter retries transient failures with backoff and
re-raises only after exhausting attempts. Non-transient errors (400/401/403)
are not retried — they won't fix themselves.
"""
from __future__ import annotations

import pytest

from simpleloop.roles import model as model_mod
from simpleloop.roles.model import ModelError, OpenAICompatChatModel, _is_transient


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


class _FakeMessage:
    def __init__(self, text: str):
        self.content = text


class _FakeChoice:
    def __init__(self, text: str):
        self.message = _FakeMessage(text)


class _FakeResponse:
    def __init__(self, text: str, usage=None):
        self.choices = [_FakeChoice(text)]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        self._owner.calls += 1
        outcome = self._owner._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeChat:
    def __init__(self, owner):
        self.completions = _FakeCompletions(owner)


class _FakeClient:
    """``client.chat.completions.create`` pops scripted outcomes in order.

    Each outcome is either an Exception (raised) or a _FakeResponse (returned).
    """
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    @property
    def chat(self):
        return _FakeChat(self)


def _model(outcomes, *, max_retries=3, retry_base_delay=0.01):
    return OpenAICompatChatModel(
        client=_FakeClient(outcomes), model="m",
        max_retries=max_retries, retry_base_delay=retry_base_delay,
    )


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


def test_is_not_transient_generic_error():
    class ParseError(Exception):
        pass
    assert _is_transient(ParseError("invalid json")) is False


# --- complete() retry behavior --------------------------------------------

def test_retry_succeeds_after_one_transient():
    model = _model([_FakeStatusError(504), _FakeResponse("ok")])
    reply = model.complete(system="s", messages=[{"role": "user", "content": "hi"}],
                           timeout_seconds=60)
    assert reply.text == "ok"
    assert model.client.calls == 2  # one failure + one success


def test_retry_succeeds_after_timeout_then_503():
    model = _model([
        _APITimeoutError(), _FakeStatusError(503), _FakeResponse("recovered"),
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
    model = _model([_FakeStatusError(400)])
    with pytest.raises(_FakeStatusError):
        model.complete(system="s", messages=[], timeout_seconds=60)
    assert model.client.calls == 1  # immediate raise, no retry


def test_no_retry_on_generic_error():
    class ParseError(Exception):
        pass
    model = _model([ParseError("bad")])
    with pytest.raises(ParseError):
        model.complete(system="s", messages=[], timeout_seconds=60)
    assert model.client.calls == 1


def test_empty_reply_is_model_error_not_retried():
    # an empty reply is a model-behavior problem, not a transient infra error —
    # it is NOT retried; the protocol-repair loop upstream owns that.
    model = _model([_FakeResponse("")])
    with pytest.raises(ModelError, match="empty"):
        model.complete(system="s", messages=[], timeout_seconds=60)
    assert model.client.calls == 1


def test_deadline_caps_retry_no_overshoot():
    # a near-zero deadline: after the first failure, no budget remains to sleep,
    # so the transient error re-raises immediately rather than overshooting.
    model = _model(
        [_FakeStatusError(504), _FakeResponse("ok")],
        max_retries=3, retry_base_delay=5.0,  # would sleep ~5s if allowed
    )
    with pytest.raises(_FakeStatusError):
        model.complete(system="s", messages=[], timeout_seconds=0.001)
    assert model.client.calls == 1  # never got to attempt 2
