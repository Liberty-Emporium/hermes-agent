"""Tests for the FAL video gen plugin — family routing, payload shape."""

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
    assert provider.default_model() == "veo3.1"


def test_fal_family_catalog():
    """Each family declares both endpoints when both modalities are supported."""
    from plugins.video_gen.fal import FAL_FAMILIES

    expected = {"veo3.1", "pixverse-v6", "kling-o3-standard"}
    assert expected.issubset(set(FAL_FAMILIES.keys()))
    for fid, meta in FAL_FAMILIES.items():
        assert meta.get("text_endpoint"), f"{fid} missing text_endpoint"
        assert meta.get("image_endpoint"), f"{fid} missing image_endpoint"
        assert meta["text_endpoint"] != meta["image_endpoint"]


def test_fal_list_models_advertises_both_modalities():
    from plugins.video_gen.fal import FALVideoGenProvider

    models = FALVideoGenProvider().list_models()
    for m in models:
        assert set(m["modalities"]) == {"text", "image"}, (
            f"{m['id']} doesn't advertise both modalities — every family "
            f"should have t2v + i2v"
        )


def test_fal_unavailable_without_key(monkeypatch):
    from plugins.video_gen.fal import FALVideoGenProvider

    monkeypatch.delenv("FAL_KEY", raising=False)
    assert FALVideoGenProvider().is_available() is False


def test_fal_generate_requires_fal_key(monkeypatch):
    from plugins.video_gen.fal import FALVideoGenProvider

    monkeypatch.delenv("FAL_KEY", raising=False)
    result = FALVideoGenProvider().generate("a happy dog")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


class TestFamilyRouting:
    """The headline behavior: image_url presence picks the endpoint."""

    @pytest.fixture
    def with_fake_fal(self, monkeypatch):
        """Stub fal_client.subscribe to capture which endpoint we hit."""
        import sys
        import types

        captured = {"endpoint": None, "arguments": None}

        fake = types.ModuleType("fal_client")
        def _subscribe(endpoint, arguments=None, with_logs=False):
            captured["endpoint"] = endpoint
            captured["arguments"] = arguments
            return {"video": {"url": "https://fake/out.mp4"}}
        fake.subscribe = _subscribe  # type: ignore
        monkeypatch.setitem(sys.modules, "fal_client", fake)

        # Reset the lazy global so it picks up our stub
        from plugins.video_gen import fal as fal_plugin
        fal_plugin._fal_client = None

        monkeypatch.setenv("FAL_KEY", "test")
        return captured

    def test_text_to_video_routes_to_text_endpoint(self, with_fake_fal):
        from plugins.video_gen.fal import FALVideoGenProvider

        result = FALVideoGenProvider().generate(
            "a dog running",
            model="pixverse-v6",
        )
        assert result["success"] is True
        assert with_fake_fal["endpoint"] == "fal-ai/pixverse/v6/text-to-video"
        assert result["modality"] == "text"
        assert with_fake_fal["arguments"]["prompt"] == "a dog running"
        assert "image_url" not in with_fake_fal["arguments"]

    def test_image_to_video_routes_to_image_endpoint(self, with_fake_fal):
        from plugins.video_gen.fal import FALVideoGenProvider

        result = FALVideoGenProvider().generate(
            "animate this dog",
            model="pixverse-v6",
            image_url="https://example.com/dog.png",
        )
        assert result["success"] is True
        assert with_fake_fal["endpoint"] == "fal-ai/pixverse/v6/image-to-video"
        assert result["modality"] == "image"
        assert with_fake_fal["arguments"]["image_url"] == "https://example.com/dog.png"

    def test_default_family_text_routing(self, with_fake_fal):
        """No model arg → DEFAULT_MODEL (veo3.1) → text-to-video endpoint."""
        from plugins.video_gen.fal import FALVideoGenProvider

        result = FALVideoGenProvider().generate("a dog")
        assert result["success"] is True
        assert with_fake_fal["endpoint"] == "fal-ai/veo3.1"

    def test_default_family_image_routing(self, with_fake_fal):
        from plugins.video_gen.fal import FALVideoGenProvider

        result = FALVideoGenProvider().generate(
            "animate this",
            image_url="https://example.com/i.png",
        )
        assert result["success"] is True
        assert with_fake_fal["endpoint"] == "fal-ai/veo3.1/image-to-video"

    def test_unknown_family_falls_back_to_default(self, with_fake_fal):
        from plugins.video_gen.fal import FALVideoGenProvider

        result = FALVideoGenProvider().generate(
            "x",
            model="not-a-real-family",
        )
        assert result["success"] is True
        # Falls back to DEFAULT_MODEL = veo3.1 text endpoint
        assert with_fake_fal["endpoint"] == "fal-ai/veo3.1"


class TestPayloadBuilder:
    def test_drops_unsupported_keys(self):
        """Veo enum-clamps duration, supports aspect+resolution+audio+neg."""
        from plugins.video_gen.fal import FAL_FAMILIES, _build_payload

        meta = FAL_FAMILIES["veo3.1"]
        p = _build_payload(
            meta,
            prompt="x",
            image_url=None,
            duration=12,           # not in enum (4,6,8) — snap to 8
            aspect_ratio="16:9",
            resolution="720p",
            negative_prompt="ugly",
            audio=True,
            seed=42,
        )
        assert p["prompt"] == "x"
        assert p["duration"] == "8"  # FAL queue API uses strings
        assert p["aspect_ratio"] == "16:9"
        assert p["resolution"] == "720p"
        assert p["generate_audio"] is True
        assert p["negative_prompt"] == "ugly"
        assert p["seed"] == 42

    def test_pixverse_range_clamps_correctly(self):
        from plugins.video_gen.fal import FAL_FAMILIES, _build_payload

        meta = FAL_FAMILIES["pixverse-v6"]
        p = _build_payload(
            meta,
            prompt="x",
            image_url="https://i.png",
            duration=99,        # over max → 15
            aspect_ratio="16:9",
            resolution="540p",
            negative_prompt=None,
            audio=None,
            seed=None,
        )
        assert p["duration"] == "15"

    def test_kling_range_clamps_correctly(self):
        from plugins.video_gen.fal import FAL_FAMILIES, _build_payload

        meta = FAL_FAMILIES["kling-o3-standard"]
        p = _build_payload(
            meta,
            prompt="x",
            image_url="https://i.png",
            duration=2,         # below min (3) → 3
            aspect_ratio="16:9",
            resolution="720p",
            negative_prompt=None,
            audio=None,
            seed=None,
        )
        assert p["duration"] == "3"
