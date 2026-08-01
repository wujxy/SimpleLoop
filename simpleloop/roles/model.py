"""Minimal chat-model boundary for the Proposer runtime."""
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


class HepAIChatModel:
    """HEPAI Chat Completions adapter; provider details stop here."""

    def __init__(self, *, client, model: str):
        self.client = client
        self.model = model

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
            raise ModelError("HEPAI returned an empty assistant message")
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return ModelReply(text=text, usage=usage)
