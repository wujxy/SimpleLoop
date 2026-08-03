"""Shared pytest fixtures for the SimpleLoop test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _seed_executor_auth_token(monkeypatch):
    """Seed the executor credential for every test.

    The loop's fail-fast guard (_assert_executor_ready) requires
    ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) so candidate execution on
    isolated worker nodes can authenticate. Tests drive _build_context with
    fakes and never call `claude` for real, but the guard still runs — so seed
    a token suite-wide. Tests that specifically exercise the guard's
    "missing credential" branch delenv both variables themselves.
    """
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
