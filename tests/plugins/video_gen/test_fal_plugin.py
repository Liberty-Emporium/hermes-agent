"""Smoke tests for the FAL video gen plugin — load, register, payload shape."""

from __future__ import annotations

import pytest

from agent import video_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


def test_fal_provider_registers():
    from plugins.video_gen.fal import FALVideoGenProvider

    provider = FALVideoGenProvider()
    video_gen_registry.register_provider(provider)

    assert video_gen_registry.get_provider("fal") is provider
    assert provider.display_name == "FAL"
    assert provider.default_model() == "fal-ai/veo3.1/image-to-video"


def test_fal_model_catalog_contains_curated_models():
    from plugins.video_gen.fal import FAL_MODELS

    expected = {
        "fal-ai/veo3.1",
        "fal-ai/veo3.1/image-to-video",
        "fal-ai/kling-video/o3/standard/image-to-video",
        "fal-ai/pixverse/v6/image-to-video",
    }
    assert expected.issubset(set(FAL_MODELS.keys()))


def test_fal_unavailable_without_key(monkeypatch):
    from plugins.video_gen.fal import FALVideoGenProvider

    monkeypatch.delenv("FAL_KEY", raising=False)
    assert FALVideoGenProvider().is_available() is False


def test_fal_rejects_unsupported_operation():
    from plugins.video_gen.fal import FALVideoGenProvider

    result = FALVideoGenProvider().generate("x", operation="edit")
    assert result["success"] is False
    assert result["error_type"] == "unsupported_operation"


def test_fal_generate_requires_fal_key(monkeypatch):
    from plugins.video_gen.fal import FALVideoGenProvider

    monkeypatch.delenv("FAL_KEY", raising=False)
    result = FALVideoGenProvider().generate("a happy dog")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_fal_image_to_video_requires_image_url(monkeypatch):
    """fal-ai/veo3.1/image-to-video (the default) is image-to-video only."""
    from plugins.video_gen.fal import FALVideoGenProvider, DEFAULT_MODEL

    assert "image-to-video" in DEFAULT_MODEL
    monkeypatch.setenv("FAL_KEY", "test")

    # fal_client absent at import time → triggers missing_dependency before
    # we ever hit the image_url check. Inject a stub so we get past import.
    import sys
    import types

    fake = types.ModuleType("fal_client")
    fake.subscribe = lambda *a, **kw: {"video": {"url": "https://x"}}
    monkeypatch.setitem(sys.modules, "fal_client", fake)

    # Re-resolve the lazy load
    from plugins.video_gen import fal as fal_plugin
    fal_plugin._fal_client = None  # force reload via our stub

    result = FALVideoGenProvider().generate("a happy dog")  # no image_url
    assert result["success"] is False
    assert result["error_type"] == "missing_image_url"


def test_fal_payload_builder_drops_unsupported_keys():
    from plugins.video_gen.fal import FAL_MODELS, _build_payload

    # veo3.1 supports negative + audio, has duration enum (4, 6, 8)
    meta = FAL_MODELS["fal-ai/veo3.1"]
    payload = _build_payload(
        meta,
        prompt="x",
        image_url=None,
        duration=12,           # not in enum — clamp to nearest (8)
        aspect_ratio="16:9",   # supported
        resolution="720p",     # supported
        negative_prompt="ugly",
        audio=True,
        seed=42,
    )
    assert payload["prompt"] == "x"
    assert payload["duration"] == "8"
    assert payload["aspect_ratio"] == "16:9"
    assert payload["resolution"] == "720p"
    assert payload["generate_audio"] is True
    assert payload["negative_prompt"] == "ugly"
    assert payload["seed"] == 42


def test_fal_pixverse_range_clamps_correctly():
    from plugins.video_gen.fal import FAL_MODELS, _build_payload

    meta = FAL_MODELS["fal-ai/pixverse/v6/image-to-video"]
    p = _build_payload(
        meta,
        prompt="x",
        image_url="https://i.png",
        duration=99,        # over max — clamp to 15
        aspect_ratio="16:9",
        resolution="540p",
        negative_prompt=None,
        audio=None,
        seed=None,
    )
    assert p["duration"] == "15"
