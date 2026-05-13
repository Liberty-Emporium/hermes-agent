"""
Video Generation Provider ABC
=============================

Defines the pluggable-backend interface for video generation. Providers register
instances via ``PluginContext.register_video_gen_provider()``; the active one
(selected via ``video_gen.provider`` in ``config.yaml``) services every
``video_generate`` tool call.

Providers live in ``<repo>/plugins/video_gen/<name>/`` (built-in, auto-loaded
as ``kind: backend``) or ``~/.hermes/plugins/video_gen/<name>/`` (user, opt-in
via ``plugins.enabled``).

Mirrors the ``image_gen`` provider design (``agent/image_gen_provider.py``) so
the two surfaces stay learnable together.

Unified surface
---------------
Video generation has more degrees of freedom than image generation, but the
core ``video_generate`` tool keeps the schema minimal and uniform. Providers
declare:

- which **operations** they support (``generate`` / ``edit`` / ``extend``)
- which **modalities** they support (text-to-video, image-to-video,
  reference-images-to-video)
- which **aspect ratios / resolutions / durations** they accept

via :meth:`VideoGenProvider.capabilities`. The tool layer uses these to clamp
or reject obviously-wrong calls before dispatch; providers are free to do
their own clamping inside :meth:`generate`. Unknown ``**kwargs`` MUST be
ignored — same forward-compat rule as image_gen.

Response shape
--------------
All providers return a dict built by :func:`success_response` /
:func:`error_response`. Keys:

    success         bool
    video           str | None      URL or absolute file path
    model           str             provider-specific model identifier
    prompt          str             echoed prompt
    operation       str             "generate" | "edit" | "extend"
    aspect_ratio    str             provider-native (e.g. "16:9") or ""
    duration        int             seconds (0 if not applicable)
    provider        str             provider name (for diagnostics)
    error           str             only when success=False
    error_type      str             only when success=False
"""

from __future__ import annotations

import abc
import base64
import datetime
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


VALID_OPERATIONS: Tuple[str, ...] = ("generate", "edit", "extend")
DEFAULT_OPERATION = "generate"

# Common aspect ratios across providers (Veo / Kling / xAI / Pixverse). The
# tool schema advertises this set as an enum hint, but providers may accept
# a narrower or wider set — they are responsible for clamping.
COMMON_ASPECT_RATIOS: Tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3")
DEFAULT_ASPECT_RATIO = "16:9"

COMMON_RESOLUTIONS: Tuple[str, ...] = ("480p", "540p", "720p", "1080p")
DEFAULT_RESOLUTION = "720p"


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class VideoGenProvider(abc.ABC):
    """Abstract base class for a video generation backend.

    Subclasses must implement :meth:`generate`. Everything else has sane
    defaults — override only what your provider needs.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in ``video_gen.provider`` config.

        Lowercase, no spaces. Examples: ``xai``, ``fal``, ``google``.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label shown in ``hermes tools``. Defaults to ``name.title()``."""
        return self.name.title()

    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        Typically checks for a required API key and optional-dependency
        import. Default: True.
        """
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        """Return catalog entries for ``hermes tools`` model picker.

        Each entry::

            {
                "id": "veo-3.1",                       # required
                "display": "Veo 3.1",                  # optional; defaults to id
                "speed": "~60s",                       # optional
                "strengths": "...",                    # optional
                "price": "$0.20/s",                    # optional
                "modalities": ["text", "image"],       # optional, info-only
                "operations": ["generate"],            # optional, info-only
            }

        Default: empty list (provider has no user-selectable models).
        """
        return []

    def get_setup_schema(self) -> Dict[str, Any]:
        """Return provider metadata for the ``hermes tools`` picker."""
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }

    def default_model(self) -> Optional[str]:
        """Return the default model id, or None if not applicable."""
        models = self.list_models()
        if models:
            return models[0].get("id")
        return None

    def capabilities(self) -> Dict[str, Any]:
        """Return what this provider supports.

        Returned dict (all keys optional)::

            {
                "operations": ["generate", "edit", "extend"],
                "modalities": ["text", "image", "reference_images"],
                "aspect_ratios": ["16:9", "9:16", ...],
                "resolutions": ["720p", "1080p"],
                "max_duration": 15,             # seconds
                "min_duration": 1,
                "supports_audio": True,
                "supports_negative_prompt": True,
                "max_reference_images": 7,
            }

        Used by the tool layer for soft validation and by ``hermes tools``
        for the picker. Default: generate + text only.
        """
        return {
            "operations": ["generate"],
            "modalities": ["text"],
            "aspect_ratios": list(COMMON_ASPECT_RATIOS),
            "resolutions": list(COMMON_RESOLUTIONS),
            "max_duration": 10,
            "min_duration": 1,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": 0,
        }

    @abc.abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        operation: str = DEFAULT_OPERATION,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        video_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate, edit, or extend a video.

        Implementations should return the dict from :func:`success_response`
        or :func:`error_response`. ``kwargs`` may contain forward-compat
        parameters future versions of the schema will expose —
        implementations MUST ignore unknown keys (no TypeError).
        """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_operation(value: Optional[str]) -> str:
    """Clamp an operation value to the valid set, defaulting to generate.

    Aliases (e.g. ``generate_video``) are accepted. Invalid values fall back
    to ``generate`` rather than raising — same forgiving posture as
    ``resolve_aspect_ratio`` on the image side.
    """
    if not isinstance(value, str):
        return DEFAULT_OPERATION
    v = value.strip().lower()
    if not v:
        return DEFAULT_OPERATION
    aliases = {
        "generate_video": "generate",
        "text_to_video": "generate",
        "txt2vid": "generate",
        "edit_video": "edit",
        "extend_video": "extend",
        "continue": "extend",
        "continuation": "extend",
    }
    v = aliases.get(v, v)
    if v in VALID_OPERATIONS:
        return v
    return DEFAULT_OPERATION


def _videos_cache_dir() -> Path:
    """Return ``$HERMES_HOME/cache/videos/``, creating parents as needed."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_b64_video(
    b64_data: str,
    *,
    prefix: str = "video",
    extension: str = "mp4",
) -> Path:
    """Decode base64 video data and write under ``$HERMES_HOME/cache/videos/``.

    Returns the absolute :class:`Path` to the saved file.

    Filename format: ``<prefix>_<YYYYMMDD_HHMMSS>_<short-uuid>.<ext>``.
    """
    raw = base64.b64decode(b64_data)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    path = _videos_cache_dir() / f"{prefix}_{ts}_{short}.{extension}"
    path.write_bytes(raw)
    return path


def save_bytes_video(
    raw: bytes,
    *,
    prefix: str = "video",
    extension: str = "mp4",
) -> Path:
    """Write raw video bytes (e.g. an HTTP download body) to the cache."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    path = _videos_cache_dir() / f"{prefix}_{ts}_{short}.{extension}"
    path.write_bytes(raw)
    return path


def success_response(
    *,
    video: str,
    model: str,
    prompt: str,
    operation: str,
    aspect_ratio: str = "",
    duration: int = 0,
    provider: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a uniform success response dict.

    ``video`` may be an HTTP URL or an absolute filesystem path.
    """
    payload: Dict[str, Any] = {
        "success": True,
        "video": video,
        "model": model,
        "prompt": prompt,
        "operation": operation,
        "aspect_ratio": aspect_ratio,
        "duration": int(duration) if duration else 0,
        "provider": provider,
    }
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)
    return payload


def error_response(
    *,
    error: str,
    error_type: str = "provider_error",
    provider: str = "",
    model: str = "",
    prompt: str = "",
    operation: str = DEFAULT_OPERATION,
    aspect_ratio: str = "",
) -> Dict[str, Any]:
    """Build a uniform error response dict."""
    return {
        "success": False,
        "video": None,
        "error": error,
        "error_type": error_type,
        "model": model,
        "prompt": prompt,
        "operation": operation,
        "aspect_ratio": aspect_ratio,
        "provider": provider,
    }
