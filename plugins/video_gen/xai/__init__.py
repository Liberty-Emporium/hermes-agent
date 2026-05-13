"""xAI Grok-Imagine video generation backend.

Salvaged from PR #10600 (@Jaaneek) and reshaped into the
:class:`VideoGenProvider` plugin interface. Original implementation kept
intact for behavior — operation routing, payload shapes, polling, and
edit/extend semantics all match #10600.

Supports:
    - text-to-video (operation='generate', prompt only)
    - image-to-video (operation='generate', prompt + image_url)
    - reference-image-guided generation (prompt + reference_image_urls)
    - video edit (operation='edit', prompt + video_url)
    - video extend (operation='extend', video_url, optional prompt)

Authentication via ``XAI_API_KEY``. Output is an HTTPS URL from xAI's
CDN; the gateway downloads and delivers it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

from agent.video_gen_provider import (
    DEFAULT_OPERATION,
    VideoGenProvider,
    error_response,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (mirrors PR #10600's tools/video_generation_tool.py)
# ---------------------------------------------------------------------------

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-imagine-video"
DEFAULT_DURATION = 8
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "720p"
DEFAULT_TIMEOUT_SECONDS = 240
DEFAULT_POLL_INTERVAL_SECONDS = 5

VALID_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
VALID_RESOLUTIONS = {"480p", "720p"}
VALID_OPERATIONS = {"generate", "edit", "extend"}
MAX_REFERENCE_IMAGES = 7


_MODELS: Dict[str, Dict[str, Any]] = {
    "grok-imagine-video": {
        "display": "Grok Imagine Video",
        "speed": "~60-240s",
        "strengths": "Generate / edit / extend; reference images; image-to-video",
        "price": "see https://docs.x.ai/docs/models",
        "modalities": ["text", "image", "reference_images"],
        "operations": ["generate", "edit", "extend"],
    },
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _xai_base_url() -> str:
    return (os.getenv("XAI_BASE_URL") or DEFAULT_XAI_BASE_URL).strip().rstrip("/")


def _xai_headers() -> Dict[str, str]:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("XAI_API_KEY not set. Get one at https://console.x.ai/")
    # Lazy import — tools.xai_http is in-tree on main but we don't want to
    # blow up if a downstream rearrangement moves it.
    try:
        from tools.xai_http import hermes_xai_user_agent

        ua = hermes_xai_user_agent()
    except Exception:
        ua = "hermes-agent/video_gen"
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": ua,
    }


def _normalize_reference_images(
    image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
):
    primary_image = None
    if image_url and image_url.strip():
        primary_image = {"url": image_url.strip()}

    refs = []
    for url in reference_image_urls or []:
        normalized = (url or "").strip()
        if normalized:
            refs.append({"url": normalized})
    return primary_image, refs or None


def _clamp_duration(
    *,
    operation: str,
    duration: Optional[int],
    has_reference_images: bool,
) -> int:
    if operation == "edit":
        # xAI inherits duration from the source video.
        return DEFAULT_DURATION
    value = duration if duration is not None else (6 if operation == "extend" else DEFAULT_DURATION)
    if value < 1:
        value = 1
    if operation == "extend":
        if value > 10:
            value = 10
    else:
        if value > 15:
            value = 15
        if has_reference_images and value > 10:
            value = 10
    return value


async def _submit(
    client: httpx.AsyncClient,
    operation: str,
    payload: Dict[str, Any],
) -> str:
    endpoint_map = {
        "generate": "videos/generations",
        "edit": "videos/edits",
        "extend": "videos/extensions",
    }
    response = await client.post(
        f"{_xai_base_url()}/{endpoint_map[operation]}",
        headers={**_xai_headers(), "x-idempotency-key": str(uuid.uuid4())},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    request_id = body.get("request_id")
    if not request_id:
        raise RuntimeError("xAI video response did not include request_id")
    return request_id


async def _poll(
    client: httpx.AsyncClient,
    request_id: str,
    *,
    timeout_seconds: int,
    poll_interval: int,
) -> Dict[str, Any]:
    elapsed = 0.0
    last_status = "queued"
    while elapsed < timeout_seconds:
        response = await client.get(
            f"{_xai_base_url()}/videos/{request_id}",
            headers=_xai_headers(),
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        last_status = (body.get("status") or "").lower()

        if last_status == "done":
            return {"status": "done", "body": body}
        if last_status in {"failed", "error", "expired", "cancelled"}:
            return {"status": last_status, "body": body}

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return {"status": "timeout", "body": {"status": last_status}}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class XAIVideoGenProvider(VideoGenProvider):
    """xAI grok-imagine-video backend."""

    @property
    def name(self) -> str:
        return "xai"

    @property
    def display_name(self) -> str:
        return "xAI"

    def is_available(self) -> bool:
        return bool(os.environ.get("XAI_API_KEY", "").strip())

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": mid, **meta} for mid, meta in _MODELS.items()]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "xAI",
            "badge": "paid",
            "tag": "grok-imagine-video — generate / edit / extend, image-to-video, reference images",
            "env_vars": [
                {
                    "key": "XAI_API_KEY",
                    "prompt": "xAI API key",
                    "url": "https://console.x.ai/",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "operations": sorted(VALID_OPERATIONS),
            "modalities": ["text", "image", "reference_images"],
            "aspect_ratios": sorted(VALID_ASPECT_RATIOS),
            "resolutions": sorted(VALID_RESOLUTIONS),
            "max_duration": 15,
            "min_duration": 1,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": MAX_REFERENCE_IMAGES,
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
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # Run the async implementation synchronously — the tool framework
        # invokes provider.generate() inside its sync handler.
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._generate_async(
                    prompt=prompt,
                    operation=operation,
                    model=model,
                    image_url=image_url,
                    video_url=video_url,
                    reference_image_urls=reference_image_urls,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                ))
            finally:
                loop.close()
        except Exception as exc:
            logger.warning("xAI video gen unexpected failure: %s", exc, exc_info=True)
            return error_response(
                error=f"xAI video generation failed: {exc}",
                error_type="api_error",
                provider="xai",
                model=model or DEFAULT_MODEL,
                prompt=prompt,
                operation=operation,
                aspect_ratio=aspect_ratio,
            )

    async def _generate_async(
        self,
        *,
        prompt: str,
        operation: str,
        model: Optional[str],
        image_url: Optional[str],
        video_url: Optional[str],
        reference_image_urls: Optional[List[str]],
        duration: Optional[int],
        aspect_ratio: str,
        resolution: str,
    ) -> Dict[str, Any]:
        if operation not in VALID_OPERATIONS:
            return error_response(
                error=f"xAI does not support operation '{operation}'",
                error_type="unsupported_operation",
                provider="xai",
                prompt=prompt,
                operation=operation,
            )

        if not os.environ.get("XAI_API_KEY", "").strip():
            return error_response(
                error="XAI_API_KEY not set. Get one at https://console.x.ai/",
                error_type="auth_required",
                provider="xai",
                prompt=prompt,
                operation=operation,
            )

        prompt = (prompt or "").strip()
        normalized_video_url = (video_url or "").strip() or None
        normalized_aspect_ratio = (aspect_ratio or DEFAULT_ASPECT_RATIO).strip()
        normalized_resolution = (resolution or DEFAULT_RESOLUTION).strip().lower()
        notes: List[str] = []

        if operation == "extend" and not prompt:
            prompt = "Continue the existing video naturally."
            notes.append("used default continuation prompt for extend")
        if operation == "edit" and not prompt:
            return error_response(
                error="prompt is required for xAI video edit",
                error_type="missing_prompt", provider="xai", operation=operation,
            )
        if operation == "generate" and not prompt and not (image_url or "").strip():
            return error_response(
                error="prompt or image_url is required for xAI text-to-video / image-to-video",
                error_type="missing_prompt", provider="xai", operation=operation,
            )

        primary_image, refs = _normalize_reference_images(image_url, reference_image_urls)
        if refs and len(refs) > MAX_REFERENCE_IMAGES:
            return error_response(
                error=f"reference_image_urls supports at most {MAX_REFERENCE_IMAGES} images on xAI",
                error_type="too_many_references", provider="xai", operation=operation,
            )

        clamped_duration = _clamp_duration(
            operation=operation,
            duration=duration,
            has_reference_images=bool(refs),
        )

        payload: Dict[str, Any] = {"model": model or DEFAULT_MODEL}
        if prompt:
            payload["prompt"] = prompt

        if operation == "generate":
            if normalized_aspect_ratio not in VALID_ASPECT_RATIOS:
                # Soft-clamp instead of rejecting — keeps the tool surface
                # forgiving when other providers pass through different
                # ratios.
                normalized_aspect_ratio = DEFAULT_ASPECT_RATIO
            if normalized_resolution not in VALID_RESOLUTIONS:
                normalized_resolution = DEFAULT_RESOLUTION
            if primary_image and refs:
                return error_response(
                    error="image_url and reference_image_urls cannot be combined on xAI",
                    error_type="conflicting_inputs",
                    provider="xai", operation=operation,
                )
            payload.update({
                "duration": clamped_duration,
                "aspect_ratio": normalized_aspect_ratio,
                "resolution": normalized_resolution,
            })
            if primary_image:
                payload["image"] = primary_image
            if refs:
                payload["reference_images"] = refs

        elif operation == "edit":
            if not normalized_video_url:
                return error_response(
                    error="video_url is required for xAI video edit",
                    error_type="missing_video_url",
                    provider="xai", operation=operation,
                )
            if primary_image or refs:
                return error_response(
                    error="image_url and reference_image_urls are not supported for xAI video edit",
                    error_type="unsupported_inputs",
                    provider="xai", operation=operation,
                )
            payload["video"] = {"url": normalized_video_url}
            notes.append("duration, aspect_ratio, resolution inherited from source video")

        else:  # extend
            if not normalized_video_url:
                return error_response(
                    error="video_url is required for xAI video extension",
                    error_type="missing_video_url",
                    provider="xai", operation=operation,
                )
            if primary_image or refs:
                return error_response(
                    error="image_url and reference_image_urls are not supported for xAI video extension",
                    error_type="unsupported_inputs",
                    provider="xai", operation=operation,
                )
            payload["duration"] = clamped_duration
            payload["video"] = {"url": normalized_video_url}

        async with httpx.AsyncClient() as client:
            try:
                request_id = await _submit(client, operation, payload)
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    detail = exc.response.text[:500]
                except Exception:
                    pass
                return error_response(
                    error=f"xAI submit failed ({exc.response.status_code}): {detail or exc}",
                    error_type="api_error",
                    provider="xai", operation=operation,
                    model=model or DEFAULT_MODEL,
                    prompt=prompt,
                )

            poll_result = await _poll(
                client, request_id,
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
            )

        status = poll_result["status"]
        body = poll_result["body"]

        if status == "done":
            video = body.get("video") or {}
            url = video.get("url")
            if not url:
                return error_response(
                    error="xAI video generation completed without a video URL",
                    error_type="empty_response",
                    provider="xai", operation=operation,
                    model=body.get("model") or model or DEFAULT_MODEL,
                    prompt=prompt,
                )
            extra: Dict[str, Any] = {
                "request_id": request_id,
                "resolution": normalized_resolution if operation == "generate" else None,
            }
            if notes:
                extra["notes"] = notes
            if body.get("usage"):
                extra["usage"] = body["usage"]
            return success_response(
                video=url,
                model=body.get("model") or model or DEFAULT_MODEL,
                prompt=prompt,
                operation=operation,
                aspect_ratio=normalized_aspect_ratio if operation == "generate" else "",
                duration=video.get("duration") or clamped_duration,
                provider="xai",
                extra=extra,
            )

        if status == "timeout":
            return error_response(
                error=f"Timed out waiting for video generation after {DEFAULT_TIMEOUT_SECONDS}s",
                error_type="timeout",
                provider="xai", operation=operation,
                model=model or DEFAULT_MODEL,
                prompt=prompt,
            )

        message = (
            (body.get("error", {}) or {}).get("message")
            or body.get("message")
            or f"xAI video generation ended with status '{status}'"
        )
        return error_response(
            error=message,
            error_type=f"xai_{status}",
            provider="xai", operation=operation,
            model=model or DEFAULT_MODEL,
            prompt=prompt,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``XAIVideoGenProvider`` into the registry."""
    ctx.register_video_gen_provider(XAIVideoGenProvider())
