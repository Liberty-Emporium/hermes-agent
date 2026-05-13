"""Tests for tools/video_generation_tool.py — dynamic schema builder.

The builder reads the user's configured backend + model and returns a
description that tells the agent which operations, modalities, and
parameters the active model actually supports. The goal: agent gets the
call right on the first turn, doesn't burn iterations on
"this model needs image_url" round-trips.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
import yaml

from agent import video_gen_registry
from agent.video_gen_provider import VideoGenProvider


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with writable config.yaml."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _write_cfg(home, cfg: dict):
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))


class _ImageOnlyProvider(VideoGenProvider):
    """Image-to-video, generate-only (like FAL's pixverse / kling i2v)."""

    @property
    def name(self) -> str:
        return "img-only"

    def is_available(self) -> bool:
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": "img-only-fast",
            "display": "Image-only Fast",
            "modality": "image",
            "operations": ["generate"],
        }]

    def default_model(self) -> Optional[str]:
        return "img-only-fast"

    def capabilities(self) -> Dict[str, Any]:
        return {
            "operations": ["generate"],
            "modalities": ["image"],
            "aspect_ratios": ["16:9", "9:16"],
            "resolutions": ["720p", "1080p"],
            "min_duration": 1,
            "max_duration": 15,
            "supports_audio": True,
            "supports_negative_prompt": True,
            "max_reference_images": 0,
        }

    def generate(self, prompt, **kwargs):
        return {"success": True}


class _FullSurfaceProvider(VideoGenProvider):
    """Generate / edit / extend, every modality (like xAI)."""

    @property
    def name(self) -> str:
        return "full"

    def is_available(self) -> bool:
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": "full-model",
            "display": "Full Model",
            "modalities": ["text", "image", "reference_images"],
            "operations": ["generate", "edit", "extend"],
        }]

    def default_model(self) -> Optional[str]:
        return "full-model"

    def capabilities(self) -> Dict[str, Any]:
        return {
            "operations": ["generate", "edit", "extend"],
            "modalities": ["text", "image", "reference_images"],
            "aspect_ratios": ["16:9", "9:16"],
            "resolutions": ["720p"],
            "min_duration": 1,
            "max_duration": 15,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": 7,
        }

    def generate(self, prompt, **kwargs):
        return {"success": True}


class _TextOnlyProvider(VideoGenProvider):
    """Text-to-video only, generate-only."""

    @property
    def name(self) -> str:
        return "text-only"

    def is_available(self) -> bool:
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": "text-only-v1",
            "modality": "text",
            "operations": ["generate"],
        }]

    def default_model(self) -> Optional[str]:
        return "text-only-v1"

    def capabilities(self) -> Dict[str, Any]:
        return {
            "operations": ["generate"],
            "modalities": ["text"],
            "supports_audio": False,
        }

    def generate(self, prompt, **kwargs):
        return {"success": True}


class TestDynamicSchemaBuilder:
    def test_no_config_falls_back_to_generic(self, cfg_home):
        from tools.video_generation_tool import _build_dynamic_video_schema

        out = _build_dynamic_video_schema()
        desc = out["description"]
        assert "No video backend is configured" in desc
        # Hint still tells the user how to fix it
        assert "hermes tools" in desc

    def test_image_only_model_advertises_image_url_required(self, cfg_home):
        from tools.video_generation_tool import _build_dynamic_video_schema

        _write_cfg(cfg_home, {"video_gen": {"provider": "img-only", "model": "img-only-fast"}})
        video_gen_registry.register_provider(_ImageOnlyProvider())

        # Bypass discovery (provider is already registered).
        import hermes_cli.plugins as plugins_module
        saved = plugins_module._ensure_plugins_discovered
        plugins_module._ensure_plugins_discovered = lambda *a, **k: None
        try:
            desc = _build_dynamic_video_schema()["description"]
        finally:
            plugins_module._ensure_plugins_discovered = saved

        # The high-signal caveat the agent needs to see
        assert "image-to-video only" in desc
        assert "image_url is REQUIRED" in desc
        assert "text-only prompts will be rejected" in desc
        # Edit/extend escalation hint
        assert "switch backends" in desc

    def test_full_surface_advertises_edit_extend_refs(self, cfg_home):
        from tools.video_generation_tool import _build_dynamic_video_schema

        _write_cfg(cfg_home, {"video_gen": {"provider": "full"}})
        video_gen_registry.register_provider(_FullSurfaceProvider())

        import hermes_cli.plugins as plugins_module
        saved = plugins_module._ensure_plugins_discovered
        plugins_module._ensure_plugins_discovered = lambda *a, **k: None
        try:
            desc = _build_dynamic_video_schema()["description"]
        finally:
            plugins_module._ensure_plugins_discovered = saved

        assert "edit, extend, generate" in desc
        assert "up to 7 images" in desc
        # No restrictive caveat — this backend supports it all
        assert "image-to-video only" not in desc
        assert "switch backends" not in desc

    def test_text_only_model_does_not_advertise_image_url(self, cfg_home):
        from tools.video_generation_tool import _build_dynamic_video_schema

        _write_cfg(cfg_home, {"video_gen": {"provider": "text-only"}})
        video_gen_registry.register_provider(_TextOnlyProvider())

        import hermes_cli.plugins as plugins_module
        saved = plugins_module._ensure_plugins_discovered
        plugins_module._ensure_plugins_discovered = lambda *a, **k: None
        try:
            desc = _build_dynamic_video_schema()["description"]
        finally:
            plugins_module._ensure_plugins_discovered = saved

        assert "text-to-video only" in desc
        assert "image_url is not supported" in desc

    def test_unknown_provider_does_not_crash(self, cfg_home):
        from tools.video_generation_tool import _build_dynamic_video_schema

        _write_cfg(cfg_home, {"video_gen": {"provider": "ghost"}})
        # No provider registered.
        import hermes_cli.plugins as plugins_module
        saved = plugins_module._ensure_plugins_discovered
        plugins_module._ensure_plugins_discovered = lambda *a, **k: None
        try:
            desc = _build_dynamic_video_schema()["description"]
        finally:
            plugins_module._ensure_plugins_discovered = saved

        # Shows the configured-but-not-loaded path; doesn't raise
        assert "ghost" in desc

    def test_builder_is_wired_into_registry(self):
        """Smoke check — confirm the registry entry has the dynamic hook."""
        from tools.registry import discover_builtin_tools, registry

        discover_builtin_tools()
        entry = registry._tools["video_generate"]
        assert entry.dynamic_schema_overrides is not None
        # And it returns a dict with a description
        out = entry.dynamic_schema_overrides()
        assert isinstance(out, dict)
        assert "description" in out
