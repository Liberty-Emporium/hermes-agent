"""Tests for the unified ``video_generate`` tool dispatch surface."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from agent import video_gen_registry
from agent.video_gen_provider import VideoGenProvider


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


class _RecordingProvider(VideoGenProvider):
    """Captures the args it was called with so tests can assert dispatch."""

    def __init__(self, name: str = "fake"):
        self._name = name
        self.last_kwargs: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": "model-a", "display": "Model A"}]

    def default_model(self) -> Optional[str]:
        return "model-a"

    def generate(self, prompt, **kwargs):
        self.last_kwargs = {"prompt": prompt, **kwargs}
        return {
            "success": True,
            "video": "https://example.com/v.mp4",
            "model": kwargs.get("model") or "model-a",
            "prompt": prompt,
            "operation": kwargs.get("operation", "generate"),
            "aspect_ratio": kwargs.get("aspect_ratio", ""),
            "duration": kwargs.get("duration") or 0,
            "provider": self._name,
        }


class _RaisingProvider(VideoGenProvider):
    @property
    def name(self) -> str:
        return "raises"

    def generate(self, prompt, **kwargs):
        raise RuntimeError("boom")


class TestUnifiedDispatch:
    def _run(self, args: Dict[str, Any], *, configured: Optional[str] = None) -> Dict[str, Any]:
        from tools import video_generation_tool

        # Force the dispatch code to see only what we register, with
        # discovery bypassed.
        import hermes_cli.plugins as plugins_module

        # Save & override.
        saved = video_generation_tool._read_configured_video_provider
        video_generation_tool._read_configured_video_provider = lambda: configured  # type: ignore
        saved_discover = plugins_module._ensure_plugins_discovered
        plugins_module._ensure_plugins_discovered = lambda *_a, **_k: None  # type: ignore
        try:
            raw = video_generation_tool._handle_video_generate(args)
        finally:
            video_generation_tool._read_configured_video_provider = saved  # type: ignore
            plugins_module._ensure_plugins_discovered = saved_discover  # type: ignore
        return json.loads(raw)

    def test_no_provider_returns_clear_error(self):
        result = self._run({"prompt": "a dog"})
        assert result["success"] is False
        assert result["error_type"] == "no_provider_configured"

    def test_unknown_provider_returns_clear_error(self):
        result = self._run({"prompt": "a dog"}, configured="ghost")
        assert result["success"] is False
        assert result["error_type"] == "provider_not_registered"
        assert "video_gen.provider='ghost'" in result["error"]

    def test_routes_prompt_and_defaults_to_provider(self):
        provider = _RecordingProvider("rec")
        video_gen_registry.register_provider(provider)
        result = self._run({"prompt": "a happy dog"})
        assert result["success"] is True
        assert provider.last_kwargs["prompt"] == "a happy dog"
        assert provider.last_kwargs["operation"] == "generate"
        # aspect_ratio + resolution defaults flow through
        assert provider.last_kwargs["aspect_ratio"] == "16:9"
        assert provider.last_kwargs["resolution"] == "720p"

    def test_image_to_video_passes_image_url(self):
        provider = _RecordingProvider("rec")
        video_gen_registry.register_provider(provider)
        self._run({
            "prompt": "animate this",
            "image_url": "https://example.com/img.png",
        })
        assert provider.last_kwargs["image_url"] == "https://example.com/img.png"

    def test_extend_requires_video_url(self):
        provider = _RecordingProvider("rec")
        video_gen_registry.register_provider(provider)
        result = self._run({"prompt": "more please", "operation": "extend"})
        # Soft validation failures come back as tool_error JSON
        # ({"error": "..."}) rather than the full error_response shape.
        assert "error" in result
        assert "video_url" in result["error"]

    def test_image_and_video_url_mutually_exclusive(self):
        provider = _RecordingProvider("rec")
        video_gen_registry.register_provider(provider)
        result = self._run({
            "prompt": "x",
            "image_url": "https://example.com/i.png",
            "video_url": "https://example.com/v.mp4",
        })
        assert "error" in result
        assert "image_url" in result["error"] or "video_url" in result["error"]

    def test_operation_aliases_normalize(self):
        provider = _RecordingProvider("rec")
        video_gen_registry.register_provider(provider)
        self._run({"prompt": "x", "operation": "generate_video"})
        assert provider.last_kwargs["operation"] == "generate"

    def test_provider_exception_caught(self):
        video_gen_registry.register_provider(_RaisingProvider())
        result = self._run({"prompt": "x"})
        assert result["success"] is False
        assert result["error_type"] == "provider_exception"
        assert result["provider"] == "raises"
