"""Phase-6 guards against retaining the monolithic RSI host."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def test_old_self_repo_module_is_absent():
    assert importlib.util.find_spec("simpleloop.self_repo") is None


def test_production_has_no_legacy_rsi_symbols():
    root = Path(__file__).resolve().parents[1] / "simpleloop"
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert "LegacyRsiRunner" not in text
    assert "class SelfRepo" not in text
