"""Minimal chat-model boundary for the Proposer runtime.

Both supported transports (HEPAI and Zhipu) speak the OpenAI Chat
Completions wire format, so the turn loop lives once on the shared base;
each subclass only owns how its client is built and which secret
environment variable authenticates it.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Protocol


class ModelError(RuntimeError):
    """The configured model transport cannot produce a usable reply."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    usage: object = None


class ChatModel(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        timeout_seconds: float,
    ) -> ModelReply:
        """Return one assistant message."""


# --- transient-error retry ------------------------------------------------
#
# A flaky upstream gateway (504 / 503 / 502) or a momentary connection drop
# must not burn a whole Scientist round — the very first call dying at nginx
# used to abstain the round outright. The OpenAI SDK retries some of these
# internally, but the HEPAI wrapper bypasses that path, so we run our own
# backoff loop around the single chat-completions call.

# HTTP status codes worth retrying (the gateway/overload/temporary family).
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _is_transient(exc: BaseException) -> bool:
    """True for gateway / connection / timeout errors worth retrying.

    ``APIStatusError`` (and ``HAPIStatusError``, which subclasses it) carries a
    clean ``status_code``; connection/timeout errors are detected by class name
    across the openai SDK, httpx, and the HEPAI wrapper. Non-transient errors
    (400/401/403/404, parse failures, empty replies) are NOT retried — they
    will not fix themselves."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _RETRY_STATUS:
        return True
    mro = " ".join(cls.__name__.lower() for cls in type(exc).__mro__)
    if "timeout" in mro or "connection" in mro:
        return True
    message = str(exc).lower()
    return any(tag in message for tag in (
        "gateway", "bad gateway", "service unavailable", "timed out",
    ))


class _RetryChatModel:
    """Shared deadline-aware retry loop for one-shot model calls.

    Transient upstream failures (504/503/502, connection drops, timeouts)
    are retried with exponential backoff — a flaky gateway must not consume
    a round. Subclasses implement ``_create`` (the provider call) and
    ``_to_reply`` (response -> ModelReply)."""

    def __init__(self, *, max_retries: int = 4, retry_base_delay: float = 2.0):
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    def _retry_delay(self, attempt: int) -> float:
        """Exponential backoff with ±25% jitter, capped at 30s."""
        delay = min(self._retry_base_delay * (2 ** attempt), 30.0)
        return delay * random.uniform(0.75, 1.25)

    def _create(self, *, system: str, messages: list[dict],
                remaining: float):
        raise NotImplementedError

    def _to_reply(self, response) -> ModelReply:
        raise NotImplementedError

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        timeout_seconds: float,
    ) -> ModelReply:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        last_exc: Exception | None = None
        response = None
        for attempt in range(self._max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelError(
                    "model call deadline exceeded"
                    + (f" (last error: {last_exc})" if last_exc else "")
                )
            try:
                response = self._create(
                    system=system, messages=messages, remaining=remaining,
                )
                break  # success — fall through to response processing
            except Exception as exc:
                last_exc = exc
                transient = _is_transient(exc)
                if not (transient and attempt < self._max_retries):
                    raise
                # cap the sleep so we never overshoot the call deadline
                delay = min(
                    self._retry_delay(attempt),
                    max(0.0, deadline - time.monotonic() - 1.0),
                )
                if delay <= 0:
                    raise
                print(
                    f"[model] transient {type(exc).__name__} "
                    f"(status={getattr(exc, 'status_code', '-')}, "
                    f"attempt {attempt + 1}/{self._max_retries}); "
                    f"retrying in {delay:.1f}s",
                    flush=True,
                )
        return self._to_reply(response)


class OpenAICompatChatModel(_RetryChatModel):
    """Chat Completions adapter for any OpenAI-compatible endpoint.

    Provider details (auth, base_url, SDK choice) stop at ``from_config``;
    the request/response logic is identical across providers."""

    def __init__(self, *, client, model: str,
                 max_retries: int = 4, retry_base_delay: float = 2.0):
        super().__init__(
            max_retries=max_retries, retry_base_delay=retry_base_delay,
        )
        self.client = client
        self.model = model

    def _create(self, *, system: str, messages: list[dict],
                remaining: float):
        return self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            stream=False,
            response_format={"type": "json_object"},
            timeout=remaining,
        )

    def _to_reply(self, response) -> ModelReply:
        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise ModelError("chat model returned an empty assistant message")
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return ModelReply(text=text, usage=usage)


class HepAIChatModel(OpenAICompatChatModel):
    """HEPAI (IHEP) Chat Completions adapter; provider details stop here."""

    @classmethod
    def from_config(cls, config: dict) -> "HepAIChatModel":
        key = os.environ.get("HEPAI_API_KEY")
        if not key:
            raise ModelError(
                "HEPAI_API_KEY is required for the HEPAI proposer"
            )
        try:
            from hepai import HepAI
        except ImportError as exc:
            raise ModelError("install the project dependency 'hepai'") from exc
        return cls(
            client=HepAI(api_key=key, base_url=config["base_url"]),
            model=config["model"],
        )


class ZhipuChatModel(OpenAICompatChatModel):
    """Zhipu (智谱) GLM Chat Completions adapter; provider details stop here.

    Zhipu exposes an OpenAI-compatible endpoint, so we drive it with the
    OpenAI SDK against ``https://open.bigmodel.cn/api/paas/v4/`` rather
    than the Anthropic-compatible ``/api/anthropic`` path (which serves the
    Messages API, not Chat Completions). GLM-4+ models honour
    ``response_format={"type": "json_object"}``.
    """

    @classmethod
    def from_config(cls, config: dict) -> "ZhipuChatModel":
        key = (
            os.environ.get("ZHIPU_API_KEY")
            or os.environ.get("ZHIPUAI_API_KEY")
        )
        if not key:
            raise ModelError(
                "ZHIPU_API_KEY (or ZHIPUAI_API_KEY) is required for the "
                "Zhipu proposer"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelError(
                "install the project dependency 'openai'"
            ) from exc
        return cls(
            client=OpenAI(api_key=key, base_url=config["base_url"]),
            model=config["model"],
        )


class AnthropicChatModel(_RetryChatModel):
    """Anthropic Messages adapter — for providers that ONLY expose the
    Messages API (e.g. bigmodel's ``/api/anthropic`` channel, whose key is
    not provisioned for the ``/api/paas/v4`` Chat Completions channel).

    Replies concatenate the ``text`` content blocks; ``thinking`` blocks
    (GLM emits them) are skipped — the Scientist protocol expects the JSON
    action in the text channel."""

    def __init__(self, *, client, model: str,
                 max_retries: int = 4, retry_base_delay: float = 2.0):
        super().__init__(
            max_retries=max_retries, retry_base_delay=retry_base_delay,
        )
        self.client = client
        self.model = model

    @classmethod
    def from_config(cls, config: dict) -> "AnthropicChatModel":
        key = (
            os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        if not key:
            raise ModelError(
                "ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) is required "
                "for the anthropic proposer"
            )
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ModelError(
                "install the project dependency 'anthropic'"
            ) from exc
        return cls(
            client=Anthropic(api_key=key, base_url=config["base_url"]),
            model=config["model"],
        )

    def _create(self, *, system: str, messages: list[dict],
                remaining: float):
        return self.client.messages.create(
            model=self.model,
            system=system,
            messages=messages,
            max_tokens=8192,
            timeout=remaining,
        )

    def _to_reply(self, response) -> ModelReply:
        text = "".join(
            block.text
            for block in (response.content or [])
            if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise ModelError("chat model returned an empty assistant message")
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return ModelReply(text=text, usage=usage)


def build_chat_model(config: dict) -> ChatModel:
    """Construct the proposer chat model for the configured ``api`` provider.

    A missing/empty ``api`` falls back to ``'hepai'`` to mirror
    ``_RESEARCHER_DEFAULTS``; resolved configs always carry an explicit api,
    so this default only matters for direct callers.
    """
    api = (config.get("api") or "hepai").strip().lower()
    if api == "hepai":
        return HepAIChatModel.from_config(config)
    if api == "zhipu":
        return ZhipuChatModel.from_config(config)
    if api == "anthropic":
        return AnthropicChatModel.from_config(config)
    raise ModelError(
        f"researcher.api: unsupported provider {api!r} "
        "(supported: 'hepai', 'zhipu', 'anthropic')"
    )
