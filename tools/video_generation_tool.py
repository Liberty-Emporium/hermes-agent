#!/usr/bin/env python3
"""
Video Generation Tool
=====================

Single ``video_generate`` tool that dispatches to a plugin-registered
video generation provider. Mirrors the ``image_generate`` design:

- ``agent/video_gen_provider.py`` defines the :class:`VideoGenProvider` ABC.
- ``agent/video_gen_registry.py`` holds the active providers (populated by
  plugins at import time).
- Each provider lives under ``plugins/video_gen/<name>/``.

The tool itself is intentionally backend-agnostic and ships **no in-tree
provider** — turn on a backend by enabling a plugin (``hermes plugins
enable video_gen/<name>``) and selecting it in ``hermes tools`` → Video
Generation.

Unified surface
---------------
One tool covers the common cases — text-to-video, image-to-video, video
edit, video extend — with a compact schema:

    prompt                   text instruction (required for generate/edit)
    operation                "generate" | "edit" | "extend"
    image_url                drives image-to-video when operation=generate
    video_url                source video for edit/extend
    reference_image_urls     list, up to provider-declared cap
    duration                 seconds (provider clamps)
    aspect_ratio             "16:9" | "9:16" | "1:1" | ...
    resolution               "480p" | "540p" | "720p" | "1080p"
    negative_prompt          optional (Pixverse/Kling style)
    audio                    optional (Veo3/Pixverse pricing tier)
    seed                     optional
    model                    optional, override the active provider's default

Providers ignore parameters they do not support. The tool layer does
**lightweight** validation (type/required-prompt) and lets each provider
do its own clamping inside :meth:`VideoGenProvider.generate` — that keeps
the tool surface stable as new providers ship with different capabilities.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from agent.video_gen_provider import (
    COMMON_ASPECT_RATIOS,
    COMMON_RESOLUTIONS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_OPERATION,
    DEFAULT_RESOLUTION,
    VALID_OPERATIONS,
    error_response,
    normalize_operation,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


VIDEO_GENERATE_SCHEMA: Dict[str, Any] = {
    "name": "video_generate",
    "description": (
        "Generate, edit, or extend a video using the user's configured video "
        "generation backend. One unified tool covers text-to-video "
        "(prompt only), image-to-video (prompt + image_url), video edit "
        "(prompt + video_url, operation='edit'), and video extension "
        "(video_url, operation='extend'). Returns either an HTTP URL or an "
        "absolute file path in the `video` field; display it with markdown "
        "![description](url-or-path) and the gateway will deliver it. The "
        "backend and model are user-configured via `hermes tools` → Video "
        "Generation; the agent does not pick them. Long-running generations "
        "may take 30 seconds to several minutes — the call blocks until the "
        "video is ready or the provider's timeout elapses."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Text instruction describing the desired video, motion, "
                    "or edit. Required for generate and edit. Optional for "
                    "extend (defaults to a natural continuation prompt)."
                ),
            },
            "operation": {
                "type": "string",
                "enum": list(VALID_OPERATIONS),
                "description": (
                    "Which video operation to perform. 'generate' makes a new "
                    "video from prompt (optionally seeded with image_url). "
                    "'edit' modifies an existing video_url according to "
                    "prompt. 'extend' continues an existing video_url. Not "
                    "every provider supports every operation — providers "
                    "surface a clear error when unsupported."
                ),
                "default": DEFAULT_OPERATION,
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Public URL of a still image to animate (image-to-video). "
                    "Used with operation='generate'. Mutually exclusive with "
                    "video_url."
                ),
            },
            "video_url": {
                "type": "string",
                "description": (
                    "Public URL of a source video for operation='edit' or "
                    "operation='extend'."
                ),
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of reference image URLs (style or "
                    "character refs). Provider-declared maximum applies — "
                    "extras are rejected by the provider."
                ),
            },
            "duration": {
                "type": "integer",
                "description": (
                    "Desired video duration in seconds. Providers clamp to "
                    "their supported range (commonly 4-15s). Omit to use the "
                    "provider's default."
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": list(COMMON_ASPECT_RATIOS),
                "description": (
                    "Output aspect ratio. Providers clamp to their supported "
                    "set."
                ),
                "default": DEFAULT_ASPECT_RATIO,
            },
            "resolution": {
                "type": "string",
                "enum": list(COMMON_RESOLUTIONS),
                "description": (
                    "Output resolution. Providers clamp to their supported "
                    "set."
                ),
                "default": DEFAULT_RESOLUTION,
            },
            "negative_prompt": {
                "type": "string",
                "description": (
                    "Optional negative prompt — content to avoid in the "
                    "output. Supported by Pixverse, Kling, and similar; "
                    "ignored by providers that do not support it."
                ),
            },
            "audio": {
                "type": "boolean",
                "description": (
                    "Optional audio generation toggle. Supported by Veo3 and "
                    "Pixverse (affects pricing tier); ignored elsewhere."
                ),
            },
            "seed": {
                "type": "integer",
                "description": (
                    "Optional seed for reproducible outputs (provider-"
                    "dependent)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model override. If omitted, the user's "
                    "configured ``video_gen.model`` (set via `hermes tools` "
                    "→ Video Generation) is used. Models that the active "
                    "provider does not know are rejected."
                ),
            },
        },
        "required": ["prompt"],
    },
}


# ---------------------------------------------------------------------------
# Config readers (mirror image_generation_tool.py)
# ---------------------------------------------------------------------------


def _read_video_gen_section() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("video_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not read video_gen config: %s", exc)
        return {}


def _read_configured_video_provider() -> Optional[str]:
    value = _read_video_gen_section().get("provider")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _read_configured_video_model() -> Optional[str]:
    value = _read_video_gen_section().get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


def check_video_generation_requirements() -> bool:
    """Return True when at least one registered provider reports available.

    Triggers plugin discovery (idempotent) so user-installed plugins are
    visible to the toolset gate.
    """
    try:
        from agent.video_gen_registry import list_providers
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        for provider in list_providers():
            try:
                if provider.is_available():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _resolve_active_provider():
    """Return the active provider object or None.

    Forces plugin discovery before checking the registry — handles cases
    where a long-lived session was started before a plugin was installed.
    """
    try:
        from agent.video_gen_registry import get_active_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        provider = get_active_provider()
        if provider is None:
            _ensure_plugins_discovered(force=True)
            provider = get_active_provider()
        return provider
    except Exception as exc:
        logger.debug("video_gen provider resolution failed: %s", exc)
        return None


def _missing_provider_error(configured: Optional[str]) -> str:
    if configured:
        msg = (
            f"video_gen.provider='{configured}' is set but no plugin "
            f"registered that name. Run `hermes plugins list` to see "
            f"installed video gen backends, or `hermes tools` → Video "
            f"Generation to pick one."
        )
        return json.dumps(error_response(
            error=msg, error_type="provider_not_registered",
            provider=configured,
        ))
    msg = (
        "No video generation backend is configured. Run `hermes tools` → "
        "Video Generation to enable one (xAI, FAL, or Google Veo)."
    )
    return json.dumps(error_response(
        error=msg, error_type="no_provider_configured",
    ))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return None


def _normalize_reference_images(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    out: List[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out or None


def _handle_video_generate(args: Dict[str, Any], **_kw: Any) -> str:
    prompt = (args.get("prompt") or "").strip()
    operation = normalize_operation(args.get("operation"))
    image_url = (args.get("image_url") or "").strip() or None
    video_url = (args.get("video_url") or "").strip() or None
    reference_image_urls = _normalize_reference_images(args.get("reference_image_urls"))
    duration = _coerce_int(args.get("duration"))
    aspect_ratio = (args.get("aspect_ratio") or DEFAULT_ASPECT_RATIO).strip() or DEFAULT_ASPECT_RATIO
    resolution = (args.get("resolution") or DEFAULT_RESOLUTION).strip() or DEFAULT_RESOLUTION
    negative_prompt = (args.get("negative_prompt") or "").strip() or None
    audio = _coerce_bool(args.get("audio"))
    seed = _coerce_int(args.get("seed"))
    model_override = (args.get("model") or "").strip() or None

    # Soft validation — providers do their own, but obvious-wrong inputs
    # get a friendly error before we dispatch.
    if operation in ("generate", "edit") and not prompt:
        # Image-to-video on grok-imagine / Pixverse can accept image-only,
        # but the unified schema treats prompt as required (matches the
        # tool schema's "required" key). Surface that clearly.
        if operation == "generate" and (image_url or video_url):
            # provider may accept this; let it through with empty prompt
            pass
        else:
            return tool_error("prompt is required for video generation")

    if operation == "extend" and not video_url:
        return tool_error("video_url is required for operation='extend'")

    if image_url and video_url:
        return tool_error(
            "image_url and video_url cannot be combined — image_url drives "
            "image-to-video generate, video_url drives edit/extend"
        )

    # Resolve the active provider.
    configured = _read_configured_video_provider()
    provider = _resolve_active_provider()
    if provider is None:
        return _missing_provider_error(configured)

    # Resolve model: explicit arg wins, then config, then provider default.
    model = model_override or _read_configured_video_model() or provider.default_model()

    kwargs: Dict[str, Any] = {
        "operation": operation,
        "model": model,
        "image_url": image_url,
        "video_url": video_url,
        "reference_image_urls": reference_image_urls,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "negative_prompt": negative_prompt,
        "audio": audio,
        "seed": seed,
    }
    # Drop None entries so providers see clean defaults.
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    try:
        result = provider.generate(prompt=prompt, **kwargs)
    except TypeError as exc:
        # A provider that hasn't widened its signature is a bug, not a
        # caller error — log and surface a clear contract message.
        logger.warning(
            "video_gen provider '%s' rejected kwargs (signature too narrow): %s",
            getattr(provider, "name", "?"), exc,
        )
        return json.dumps(error_response(
            error=(
                f"Provider '{getattr(provider, 'name', '?')}' signature is "
                f"out of date with the video_generate schema. Report this "
                f"to the plugin author."
            ),
            error_type="provider_contract",
            provider=getattr(provider, "name", ""),
            model=model or "",
            prompt=prompt,
            operation=operation,
        ))
    except Exception as exc:
        logger.warning(
            "video_gen provider '%s' raised: %s",
            getattr(provider, "name", "?"), exc,
        )
        return json.dumps(error_response(
            error=f"Provider '{getattr(provider, 'name', '?')}' error: {exc}",
            error_type="provider_exception",
            provider=getattr(provider, "name", ""),
            model=model or "",
            prompt=prompt,
            operation=operation,
        ))

    if not isinstance(result, dict):
        return json.dumps(error_response(
            error="Provider returned a non-dict result",
            error_type="provider_contract",
            provider=getattr(provider, "name", ""),
            model=model or "",
            prompt=prompt,
            operation=operation,
        ))

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


registry.register(
    name="video_generate",
    toolset="video_gen",
    schema=VIDEO_GENERATE_SCHEMA,
    handler=_handle_video_generate,
    check_fn=check_video_generation_requirements,
    requires_env=[],
    is_async=False,
    emoji="🎬",
)
