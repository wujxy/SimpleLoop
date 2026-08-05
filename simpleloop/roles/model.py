"""Minimal chat-model boundary for the Proposer runtime.

Both supported transports (HEPAI and Zhipu) speak the OpenAI Chat
Completions wire format, so the turn loop lives once on the shared base;
each subclass only owns how its client is built and which secret
environment variable authenticates it.
"""
from __future__ import annotations

import os
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


class OpenAICompatChatModel:
    """Chat Completions adapter for any OpenAI-compatible endpoint.

    Provider details (auth, base_url, SDK choice) stop at ``from_config``;
    the request/response logic is identical across providers.
    """

    def __init__(self, *, client, model: str):
        self.client = client
        self.model = model

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        timeout_seconds: float,
    ) -> ModelReply:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            stream=False,
            response_format={"type": "json_object"},
            timeout=timeout_seconds,
        )
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
    raise ModelError(
        f"researcher.api: unsupported provider {api!r} "
        "(supported: 'hepai', 'zhipu')"
    )
