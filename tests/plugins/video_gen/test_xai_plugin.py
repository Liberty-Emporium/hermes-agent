"""Smoke tests for the xAI video gen plugin — load & register surface only.

Full integration is gated on a real XAI_API_KEY and not run in CI.
"""

from __future__ import annotations

import pytest

from agent import video_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


def test_xai_provider_registers():
    from plugins.video_gen.xai import XAIVideoGenProvider

    provider = XAIVideoGenProvider()
    video_gen_registry.register_provider(provider)

    fetched = video_gen_registry.get_provider("xai")
    assert fetched is provider
    assert provider.display_name == "xAI"
    assert provider.default_model() == "grok-imagine-video"
    assert "grok-imagine-video" in {m["id"] for m in provider.list_models()}


def test_xai_capabilities():
    from plugins.video_gen.xai import XAIVideoGenProvider

    caps = XAIVideoGenProvider().capabilities()
    assert "generate" in caps["operations"]
    assert "edit" in caps["operations"]
    assert "extend" in caps["operations"]
    assert caps["max_reference_images"] == 7


def test_xai_unavailable_without_key(monkeypatch):
    from plugins.video_gen.xai import XAIVideoGenProvider

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert XAIVideoGenProvider().is_available() is False


def test_xai_generate_requires_xai_key(monkeypatch):
    """Calling generate() without XAI_API_KEY returns a clean error,
    not an exception."""
    from plugins.video_gen.xai import XAIVideoGenProvider

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    result = XAIVideoGenProvider().generate("a happy dog")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_xai_rejects_unsupported_operation():
    from plugins.video_gen.xai import XAIVideoGenProvider

    result = XAIVideoGenProvider().generate("x", operation="dance")
    assert result["success"] is False
    assert result["error_type"] == "unsupported_operation"
