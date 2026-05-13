"""FAL.ai video generation backend.

Multi-model FAL backend covering text-to-video and image-to-video. Models
are user-selectable via ``hermes tools`` → Video Generation → FAL; the
choice is persisted to ``video_gen.model`` in ``config.yaml``.

Model catalog (curated initial set — more can be added without code
changes by users via ``FAL_VIDEO_MODEL``):

    fal-ai/veo3.1                                  Veo 3.1, text-to-video
    fal-ai/veo3.1/image-to-video                   Veo 3.1, image-to-video
    fal-ai/kling-video/o3/standard/image-to-video  Kling O3 standard i2v
    fal-ai/pixverse/v6/image-to-video              Pixverse v6 i2v

Selection precedence:
    1. ``model=`` arg from the tool call
    2. ``FAL_VIDEO_MODEL`` env var
    3. ``video_gen.fal.model`` in ``config.yaml``
    4. ``video_gen.model`` in ``config.yaml`` (when it's one of our IDs)
    5. ``DEFAULT_MODEL``

Authentication via ``FAL_KEY``. Output is an HTTPS URL from FAL's CDN; the
gateway downloads and delivers it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.video_gen_provider import (
    DEFAULT_OPERATION,
    VideoGenProvider,
    error_response,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
#
# Each entry declares:
#   modality       : 'text' | 'image' | 'either'
#   aspect_ratios  : tuple of supported ratios (None = model decides)
#   resolutions    : tuple of supported resolutions (None = model decides)
#   durations      : tuple of supported durations OR (min, max) range
#   audio          : True if generate_audio is supported
#   negative       : True if negative_prompt is supported
#   refs           : True if reference_image is/are supported
#
# Capabilities here are derived from FAL's model pages. Models that lie
# about their schema (or that FAL updates without notice) still work —
# unknown keys get filtered by `_build_payload` per-model.

FAL_MODELS: Dict[str, Dict[str, Any]] = {
    "fal-ai/veo3.1": {
        "display": "Veo 3.1 (text-to-video)",
        "speed": "~60-120s",
        "price": "$0.20-0.40/s",
        "strengths": "Google DeepMind. Cinematic, native audio, strong prompt adherence.",
        "modality": "text",
        "aspect_ratios": ("16:9", "9:16"),
        "resolutions": ("720p", "1080p"),
        "durations": (4, 6, 8),
        "audio": True,
        "negative": True,
        "refs": False,
    },
    "fal-ai/veo3.1/image-to-video": {
        "display": "Veo 3.1 (image-to-video)",
        "speed": "~60-120s",
        "price": "$0.20-0.40/s",
        "strengths": "Animate an input image. Native audio support.",
        "modality": "image",
        "aspect_ratios": ("16:9", "9:16"),
        "resolutions": ("720p", "1080p"),
        "durations": (4, 6, 8),
        "audio": True,
        "negative": True,
        "refs": False,
    },
    "fal-ai/kling-video/o3/standard/image-to-video": {
        "display": "Kling O3 Standard (image-to-video)",
        "speed": "~60-180s",
        "price": "$0.20-0.40/s",
        "strengths": "Start/end frame, multi-shot, native audio, 3-15s.",
        "modality": "image",
        "aspect_ratios": None,
        "resolutions": ("720p", "1080p"),
        "durations": (3, 15),  # range
        "audio": True,
        "negative": True,
        "refs": False,
    },
    "fal-ai/pixverse/v6/image-to-video": {
        "display": "Pixverse v6 (image-to-video)",
        "speed": "~30-90s",
        "price": "$0.025-0.115/s",
        "strengths": "Affordable. Negative prompts. 1-15s durations.",
        "modality": "image",
        "aspect_ratios": None,
        "resolutions": ("360p", "540p", "720p", "1080p"),
        "durations": (1, 15),
        "audio": True,
        "negative": True,
        "refs": False,
    },
}

DEFAULT_MODEL = "fal-ai/veo3.1/image-to-video"


def _is_duration_range(durations: Any) -> bool:
    """A tuple of exactly two integers is treated as ``(min, max)`` range."""
    return (
        isinstance(durations, tuple)
        and len(durations) == 2
        and all(isinstance(d, int) for d in durations)
        and not (durations[0] in durations[1:])  # avoid collision with enum (a,b)
    ) or (
        isinstance(durations, tuple) and len(durations) == 2 and durations[0] < durations[1]
        and durations[1] - durations[0] > 1  # heuristic: (3,15) is range, (4,8) is enum
    )


def _clamp_duration(model_meta: Dict[str, Any], duration: Optional[int]) -> Optional[int]:
    durations = model_meta.get("durations")
    if not durations:
        return duration
    if duration is None:
        # default — pick the smallest supported
        if _is_duration_range(durations):
            return durations[0]
        return durations[0]
    if _is_duration_range(durations):
        lo, hi = durations
        return max(lo, min(hi, duration))
    # enum
    if duration in durations:
        return duration
    # snap to nearest enum value
    return min(durations, key=lambda d: abs(d - duration))


# ---------------------------------------------------------------------------
# Config / model resolution
# ---------------------------------------------------------------------------


def _load_video_gen_section() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("video_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load video_gen config: %s", exc)
        return {}


def _resolve_model(explicit: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """Decide which FAL model to use. Returns ``(model_id, meta)``."""
    candidates: List[Optional[str]] = []
    candidates.append(explicit)
    candidates.append(os.environ.get("FAL_VIDEO_MODEL"))

    cfg = _load_video_gen_section()
    fal_cfg = cfg.get("fal") if isinstance(cfg.get("fal"), dict) else {}
    if isinstance(fal_cfg, dict):
        candidates.append(fal_cfg.get("model"))
    top = cfg.get("model")
    if isinstance(top, str):
        candidates.append(top)

    for c in candidates:
        if isinstance(c, str) and c.strip() and c.strip() in FAL_MODELS:
            mid = c.strip()
            return mid, FAL_MODELS[mid]

    return DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL]


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def _build_payload(
    model_meta: Dict[str, Any],
    *,
    prompt: str,
    image_url: Optional[str],
    duration: Optional[int],
    aspect_ratio: str,
    resolution: str,
    negative_prompt: Optional[str],
    audio: Optional[bool],
    seed: Optional[int],
) -> Dict[str, Any]:
    """Build a model-specific payload, dropping unsupported keys.

    Mirrors the FAL_MODELS metadata: keys the model does not declare
    support for are simply omitted (forward-compat with future FAL schema
    changes — we never send rejected keys).
    """
    payload: Dict[str, Any] = {}

    if prompt:
        payload["prompt"] = prompt
    if image_url:
        payload["image_url"] = image_url
    if seed is not None:
        payload["seed"] = seed

    if model_meta.get("aspect_ratios"):
        if aspect_ratio in model_meta["aspect_ratios"]:
            payload["aspect_ratio"] = aspect_ratio
        # otherwise let the model auto-crop / use its default

    if model_meta.get("resolutions"):
        if resolution in model_meta["resolutions"]:
            payload["resolution"] = resolution
        # else: let the model default

    clamped = _clamp_duration(model_meta, duration)
    if clamped is not None and model_meta.get("durations"):
        # FAL's Veo and Kling/Pixverse both expose duration as a string in
        # the queue API ("8" not 8). Keep it stringly typed for safety —
        # the JSON serializer would have produced the same shape either
        # way for new schemas.
        payload["duration"] = str(clamped)

    if model_meta.get("audio") and audio is not None:
        payload["generate_audio"] = bool(audio)

    if model_meta.get("negative") and negative_prompt:
        payload["negative_prompt"] = negative_prompt

    return payload


# ---------------------------------------------------------------------------
# fal_client lazy import (same pattern as image_generation_tool)
# ---------------------------------------------------------------------------

_fal_client: Any = None


def _load_fal_client() -> Any:
    global _fal_client
    if _fal_client is not None:
        return _fal_client
    import fal_client  # type: ignore

    _fal_client = fal_client
    return fal_client


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class FALVideoGenProvider(VideoGenProvider):
    """FAL.ai multi-model video generation backend."""

    @property
    def name(self) -> str:
        return "fal"

    @property
    def display_name(self) -> str:
        return "FAL"

    def is_available(self) -> bool:
        if not os.environ.get("FAL_KEY", "").strip():
            return False
        try:
            import fal_client  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for mid, meta in FAL_MODELS.items():
            ops = ["generate"]
            modalities = ["text"] if meta["modality"] == "text" else (
                ["image"] if meta["modality"] == "image" else ["text", "image"]
            )
            out.append({
                "id": mid,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
                "modalities": modalities,
                "operations": ops,
            })
        return out

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "FAL",
            "badge": "paid",
            "tag": "Veo 3.1, Kling, Pixverse — text-to-video & image-to-video",
            "env_vars": [
                {
                    "key": "FAL_KEY",
                    "prompt": "FAL.ai API key",
                    "url": "https://fal.ai/dashboard/keys",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "operations": ["generate"],
            "modalities": ["text", "image"],
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "resolutions": ["360p", "540p", "720p", "1080p"],
            "max_duration": 15,
            "min_duration": 1,
            "supports_audio": True,
            "supports_negative_prompt": True,
            "max_reference_images": 0,
        }

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
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if operation != "generate":
            return error_response(
                error=(
                    f"FAL backend does not support operation='{operation}'. "
                    f"For video edit/extend, switch to xAI via "
                    f"`hermes tools` → Video Generation."
                ),
                error_type="unsupported_operation",
                provider="fal", operation=operation,
                prompt=prompt,
            )

        if not os.environ.get("FAL_KEY", "").strip():
            return error_response(
                error=(
                    "FAL_KEY not set. Run `hermes tools` → Video Generation "
                    "→ FAL to configure."
                ),
                error_type="auth_required",
                provider="fal", operation=operation,
                prompt=prompt,
            )

        try:
            fal_client = _load_fal_client()
        except ImportError:
            return error_response(
                error="fal_client Python package not installed (pip install fal-client)",
                error_type="missing_dependency",
                provider="fal", operation=operation,
                prompt=prompt,
            )

        prompt = (prompt or "").strip()
        model_id, meta = _resolve_model(model)

        if meta["modality"] == "image" and not (image_url and image_url.strip()):
            return error_response(
                error=(
                    f"Model {model_id} is an image-to-video model; image_url "
                    f"is required."
                ),
                error_type="missing_image_url",
                provider="fal", operation=operation,
                model=model_id, prompt=prompt,
            )
        if meta["modality"] == "text" and not prompt:
            return error_response(
                error=f"Model {model_id} is text-to-video; prompt is required.",
                error_type="missing_prompt",
                provider="fal", operation=operation,
                model=model_id, prompt=prompt,
            )

        payload = _build_payload(
            meta,
            prompt=prompt,
            image_url=(image_url or "").strip() or None,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            negative_prompt=negative_prompt,
            audio=audio,
            seed=seed,
        )

        try:
            os.environ["FAL_KEY"] = os.environ["FAL_KEY"]  # noqa: idempotent
            result = fal_client.subscribe(
                model_id,
                arguments=payload,
                with_logs=False,
            )
        except Exception as exc:
            logger.warning("FAL video gen failed (%s): %s", model_id, exc, exc_info=True)
            return error_response(
                error=f"FAL video generation failed: {exc}",
                error_type="api_error",
                provider="fal", operation=operation,
                model=model_id, prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        video = (result or {}).get("video") if isinstance(result, dict) else None
        url: Optional[str] = None
        if isinstance(video, dict):
            url = video.get("url")
        elif isinstance(video, str):
            url = video

        if not url:
            return error_response(
                error="FAL returned no video URL in response",
                error_type="empty_response",
                provider="fal", operation=operation,
                model=model_id, prompt=prompt,
            )

        extra: Dict[str, Any] = {}
        if isinstance(video, dict):
            if video.get("file_size"):
                extra["file_size"] = video["file_size"]
            if video.get("content_type"):
                extra["content_type"] = video["content_type"]

        return success_response(
            video=url,
            model=model_id,
            prompt=prompt,
            operation=operation,
            aspect_ratio=aspect_ratio if "aspect_ratio" in payload else "",
            duration=int(payload["duration"]) if "duration" in payload else 0,
            provider="fal",
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``FALVideoGenProvider`` into the registry."""
    ctx.register_video_gen_provider(FALVideoGenProvider())
