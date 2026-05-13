---
sidebar_position: 12
title: "Video Generation Provider Plugins"
description: "How to build a video-generation backend plugin for Hermes Agent"
---

# Building a Video Generation Provider Plugin

Video-gen provider plugins register a backend that services every `video_generate` tool call — xAI Grok-Imagine, FAL.ai (Veo / Kling / Pixverse), Runway, Pika, ComfyUI rigs, anything. Built-in providers (xAI, FAL) ship as plugins. Add a new one, or override a bundled one, by dropping a directory into `plugins/video_gen/<name>/`.

:::tip
Video-gen mirrors [Image Generation Provider Plugins](/docs/developer-guide/image-gen-provider-plugin) almost line-for-line — if you've built an image-gen backend, you already know the shape. The differences are: more operations (`generate` / `edit` / `extend`), more modalities (text / image / reference-images), and richer capability metadata.
:::

## How discovery works

Hermes scans for video-gen backends in three places:

1. **Bundled** — `<repo>/plugins/video_gen/<name>/` (auto-loaded with `kind: backend`, always available)
2. **User** — `~/.hermes/plugins/video_gen/<name>/` (opt-in via `plugins.enabled`)
3. **Pip** — packages declaring a `hermes_agent.plugins` entry point

Each plugin's `register(ctx)` function calls `ctx.register_video_gen_provider(...)` — that puts it into the registry in `agent/video_gen_registry.py`. The active provider is picked by `video_gen.provider` in `config.yaml`; `hermes tools` → Video Generation walks users through selection.

The `video_generate` tool wrapper asks the registry for the active provider and dispatches there. If no provider is registered, the tool surfaces a helpful error pointing at `hermes tools`. Unlike `image_generate`, there is no in-tree legacy backend — every provider is a plugin.

## Directory structure

```
plugins/video_gen/my-backend/
├── __init__.py      # VideoGenProvider subclass + register()
└── plugin.yaml      # Manifest with kind: backend
```

## The VideoGenProvider ABC

Subclass `agent.video_gen_provider.VideoGenProvider`. The only required members are the `name` property and the `generate()` method — everything else has sane defaults:

```python
# plugins/video_gen/my-backend/__init__.py
from typing import Any, Dict, List, Optional
import os

from agent.video_gen_provider import (
    DEFAULT_OPERATION,
    VideoGenProvider,
    error_response,
    success_response,
)


class MyVideoGenProvider(VideoGenProvider):
    @property
    def name(self) -> str:
        return "my-backend"

    @property
    def display_name(self) -> str:
        return "My Backend"

    def is_available(self) -> bool:
        return bool(os.environ.get("MY_API_KEY"))

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "fast",
                "display": "Fast",
                "speed": "~30s",
                "strengths": "Cheapest tier",
                "price": "$0.05/s",
                "modalities": ["text", "image"],
                "operations": ["generate"],
            },
        ]

    def default_model(self) -> Optional[str]:
        return "fast"

    def capabilities(self) -> Dict[str, Any]:
        return {
            "operations": ["generate"],
            "modalities": ["text", "image"],
            "aspect_ratios": ["16:9", "9:16"],
            "resolutions": ["720p", "1080p"],
            "max_duration": 10,
            "min_duration": 1,
            "supports_audio": False,
            "supports_negative_prompt": True,
            "max_reference_images": 0,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "My Backend",
            "badge": "paid",
            "tag": "Short description shown in `hermes tools`",
            "env_vars": [
                {
                    "key": "MY_API_KEY",
                    "prompt": "My Backend API key",
                    "url": "https://mybackend.example.com/keys",
                },
            ],
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
        # Always ignore unknown kwargs — keeps your plugin forward-compatible
        # with future schema additions.
        if operation != "generate":
            return error_response(
                error=f"my-backend does not support operation='{operation}'",
                error_type="unsupported_operation",
                provider=self.name, operation=operation, prompt=prompt,
            )

        # ... call your API, save/return URL ...
        return success_response(
            video="https://your-cdn/output.mp4",
            model=model or "fast",
            prompt=prompt,
            operation=operation,
            aspect_ratio=aspect_ratio,
            duration=duration or 5,
            provider=self.name,
        )


def register(ctx) -> None:
    ctx.register_video_gen_provider(MyVideoGenProvider())
```

## The plugin manifest

```yaml
# plugins/video_gen/my-backend/plugin.yaml
name: my-backend
version: 1.0.0
description: "My video generation backend"
author: Your Name
kind: backend
requires_env:
  - MY_API_KEY
```

## The unified `video_generate` schema

The tool exposes one schema across every backend. Providers ignore parameters they don't support — there's no requirement that every backend implement every option.

| Parameter | What it does | Required? |
|---|---|---|
| `prompt` | Text instruction | Required for `generate`/`edit`, optional for `extend` |
| `operation` | `generate` / `edit` / `extend` | Default `generate` |
| `image_url` | Animate this still (image-to-video) | Use with `operation=generate` |
| `video_url` | Source video for edit/extend | Required for `edit`/`extend` |
| `reference_image_urls` | Style or character refs | Optional, up to provider's cap |
| `duration` | Seconds | Provider clamps |
| `aspect_ratio` | `"16:9"`, `"9:16"`, `"1:1"`, ... | Provider clamps |
| `resolution` | `"480p"` / `"540p"` / `"720p"` / `"1080p"` | Provider clamps |
| `negative_prompt` | Content to avoid | Pixverse/Kling only |
| `audio` | Generate native audio | Veo3 / Pixverse pricing tier |
| `seed` | Reproducibility | Provider-dependent |
| `model` | Override the active model | Optional |

The provider's `capabilities()` advertises which of these are honored; `hermes tools` uses it to inform the user, and the tool layer uses it to short-circuit obviously-unsupported combinations.

## Selection precedence

A provider that wants its own per-instance model knob should follow this order (see `plugins/video_gen/fal/__init__.py` for a working example):

1. `model=` keyword from the tool call
2. `<PROVIDER>_VIDEO_MODEL` env var
3. `video_gen.<provider>.model` in `config.yaml`
4. `video_gen.model` in `config.yaml` (when it's one of your IDs)
5. Provider's `default_model()`

## Response shape

`success_response()` and `error_response()` produce the dict shape every backend returns. The tool wrapper JSON-serializes it. Use them — don't hand-roll the dict.

Success keys: `success`, `video` (URL or absolute path), `model`, `prompt`, `operation`, `aspect_ratio`, `duration`, `provider`, plus whatever you pass in `extra`.

Error keys: `success`, `video` (None), `error`, `error_type`, `model`, `prompt`, `operation`, `aspect_ratio`, `provider`.

## Where to save artifacts

If your backend returns base64 (xAI's edit/extend, some FAL models in some modes), use `save_b64_video()` to write under `$HERMES_HOME/cache/videos/`. For raw bytes (from a follow-up HTTP fetch), use `save_bytes_video()`. Both return an absolute `Path`. Otherwise return the upstream URL directly — the gateway resolves remote URLs on delivery.

## Testing

Drop a smoke test under `tests/plugins/video_gen/test_<name>_plugin.py`. The bundled tests for xAI and FAL show the pattern: register the provider, verify catalog entries, assert `is_available()` toggles on env var presence, and confirm clean error responses when called without keys.
