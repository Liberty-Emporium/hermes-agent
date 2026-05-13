"""Integration tests for the xAI video gen plugin's full surface.

Each of xAI's documented modes (text-to-video, image-to-video,
reference-images-to-video, video edit, video extend) round-trips through
the plugin with httpx stubbed out. We assert the endpoint hit and the
payload shape — not just the success/error response — because endpoint
routing is the part most likely to break silently.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any, Dict, List, Optional

import pytest

from agent import video_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


class _FakeResponse:
    def __init__(self, status: int = 200, payload: Optional[Dict[str, Any]] = None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Captures POST URL+payload, returns a done video on GET."""

    def __init__(self):
        self.posts: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return _FakeResponse(200, {"request_id": "req-123"})

    async def get(self, url, headers=None, timeout=None):
        return _FakeResponse(200, {
            "status": "done",
            "video": {
                "url": "https://xai-cdn/out.mp4",
                "duration": 8,
            },
            "model": "grok-imagine-video",
        })


@pytest.fixture
def xai_provider(monkeypatch):
    """Set up the xAI plugin with httpx stubbed and a fake API key."""
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    # Plumb the fake client + skip polling delay.
    import plugins.video_gen.xai as xai_plugin

    captured: Dict[str, _FakeAsyncClient] = {}

    def _client_factory():
        captured["client"] = _FakeAsyncClient()
        return captured["client"]

    monkeypatch.setattr(xai_plugin.httpx, "AsyncClient", _client_factory)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    provider = xai_plugin.XAIVideoGenProvider()
    return provider, captured


def _last_post(captured) -> Dict[str, Any]:
    return captured["client"].posts[-1]


class TestXAIEndpointRouting:
    """Operation → endpoint mapping. The most important invariant."""

    def test_generate_hits_generations_endpoint(self, xai_provider):
        provider, captured = xai_provider
        result = provider.generate("a dog on a skateboard")
        assert result["success"] is True
        assert _last_post(captured)["url"].endswith("/videos/generations")

    def test_edit_hits_edits_endpoint(self, xai_provider):
        provider, captured = xai_provider
        result = provider.generate(
            "add rain",
            operation="edit",
            video_url="https://example.com/src.mp4",
        )
        assert result["success"] is True
        assert _last_post(captured)["url"].endswith("/videos/edits")

    def test_extend_hits_extensions_endpoint(self, xai_provider):
        provider, captured = xai_provider
        result = provider.generate(
            "continue with the dog running",
            operation="extend",
            video_url="https://example.com/src.mp4",
        )
        assert result["success"] is True
        assert _last_post(captured)["url"].endswith("/videos/extensions")


class TestXAIModalities:
    """text / image / reference_images / video_url → payload shape."""

    def test_text_to_video_payload(self, xai_provider):
        provider, captured = xai_provider
        provider.generate("a dog at sunset")
        payload = _last_post(captured)["json"]
        assert payload["prompt"] == "a dog at sunset"
        assert "image" not in payload
        assert "reference_images" not in payload
        assert "video" not in payload

    def test_image_to_video_payload(self, xai_provider):
        provider, captured = xai_provider
        provider.generate("animate this", image_url="https://example.com/cat.png")
        payload = _last_post(captured)["json"]
        assert payload["image"] == {"url": "https://example.com/cat.png"}
        assert "reference_images" not in payload

    def test_reference_images_payload(self, xai_provider):
        provider, captured = xai_provider
        provider.generate(
            "keep this character",
            reference_image_urls=[
                "https://example.com/a.png",
                "https://example.com/b.png",
            ],
        )
        payload = _last_post(captured)["json"]
        assert payload["reference_images"] == [
            {"url": "https://example.com/a.png"},
            {"url": "https://example.com/b.png"},
        ]
        assert "image" not in payload

    def test_edit_payload_has_video_url(self, xai_provider):
        provider, captured = xai_provider
        provider.generate(
            "add rain",
            operation="edit",
            video_url="https://example.com/src.mp4",
        )
        payload = _last_post(captured)["json"]
        assert payload["video"] == {"url": "https://example.com/src.mp4"}


class TestXAIValidation:
    """Client-side rejections — these should never hit the network."""

    def test_edit_without_prompt_rejects(self, xai_provider):
        provider, captured = xai_provider
        result = provider.generate(
            "",
            operation="edit",
            video_url="https://example.com/src.mp4",
        )
        assert result["success"] is False
        assert result["error_type"] == "missing_prompt"
        # Did NOT hit the network
        assert "client" not in captured or not captured["client"].posts

    def test_extend_without_video_url_rejects(self, xai_provider):
        provider, captured = xai_provider
        result = provider.generate(
            "more please",
            operation="extend",
        )
        assert result["success"] is False
        assert result["error_type"] == "missing_video_url"
        assert "client" not in captured or not captured["client"].posts

    def test_image_plus_refs_rejects(self, xai_provider):
        provider, captured = xai_provider
        result = provider.generate(
            "x",
            image_url="https://example.com/i.png",
            reference_image_urls=["https://example.com/r.png"],
        )
        assert result["success"] is False
        assert result["error_type"] == "conflicting_inputs"
        assert "client" not in captured or not captured["client"].posts

    def test_too_many_references_rejects(self, xai_provider):
        provider, captured = xai_provider
        result = provider.generate(
            "x",
            reference_image_urls=[f"https://example.com/r{i}.png" for i in range(8)],
        )
        assert result["success"] is False
        assert result["error_type"] == "too_many_references"
        assert "client" not in captured or not captured["client"].posts

    def test_extend_without_prompt_auto_fills_default(self, xai_provider):
        """xAI extend without a prompt is legal — the plugin substitutes
        a continuation default rather than rejecting. This is the
        documented behavior from the source PR (#10600)."""
        provider, captured = xai_provider
        result = provider.generate(
            "",
            operation="extend",
            video_url="https://example.com/src.mp4",
        )
        assert result["success"] is True
        payload = _last_post(captured)["json"]
        assert "Continue" in payload["prompt"]


class TestXAIClamping:
    """Per-mode duration / aspect ratio clamping."""

    def test_generate_duration_clamped_to_15(self, xai_provider):
        provider, captured = xai_provider
        provider.generate("x", duration=30)
        assert _last_post(captured)["json"]["duration"] == 15

    def test_extend_duration_clamped_to_10(self, xai_provider):
        provider, captured = xai_provider
        provider.generate(
            "x", operation="extend",
            video_url="https://example.com/v.mp4",
            duration=20,
        )
        assert _last_post(captured)["json"]["duration"] == 10

    def test_invalid_aspect_ratio_soft_clamps(self, xai_provider):
        provider, captured = xai_provider
        provider.generate("x", aspect_ratio="21:9")  # not in xAI's enum
        assert _last_post(captured)["json"]["aspect_ratio"] == "16:9"
