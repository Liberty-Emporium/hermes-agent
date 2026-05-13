---
title: Codex App-Server Runtime (optional)
sidebar_label: Codex App-Server Runtime
---

# Codex App-Server Runtime

Hermes can optionally hand `openai/*` and `openai-codex/*` turns to the [Codex CLI app-server](https://github.com/openai/codex) instead of running its own tool loop. When enabled, terminal commands, file edits, sandboxing, and MCP tool calls all execute inside Codex's runtime — Hermes becomes the shell around it (sessions DB, slash commands, gateway, memory and skill review).

This is **opt-in only**. Default Hermes behavior is unchanged unless you flip the flag. Hermes never auto-routes you onto this runtime.

## Why

- Run OpenAI agent turns against your **ChatGPT subscription** (no API key required) using the same auth flow Codex CLI uses.
- Use **Codex's own toolset and sandbox** — `apply_patch` with Codex's diff format, seatbelt/landlock sandboxing, native shell execution.
- **Native Codex plugins** — Linear, GitHub, Gmail, Calendar, Canva, etc. — installed via `codex plugin` are auto-migrated and active in your Hermes session.
- **Hermes' richer tools come along** — web_search, web_extract, browser automation, vision, image generation, skills, and TTS work via an MCP callback. Codex calls back into Hermes for tools it doesn't have built in.
- **Memory and skill nudges keep working** — Codex's events are projected into Hermes' message shape so the self-improvement loop sees a normal-looking transcript.

## Trade-offs

|  | Hermes default runtime | Codex app-server (opt-in) |
|---|---|---|
| `delegate_task` subagents | yes | not available — needs agent loop context |
| `memory`, `session_search`, `todo` | yes | not available — needs agent loop context |
| `web_search`, `web_extract` | yes | yes (via MCP callback) |
| Browser automation (Camofox/Browserbase) | yes | yes (via MCP callback) |
| `vision_analyze`, `image_generate` | yes | yes (via MCP callback) |
| `skill_view`, `skills_list` | yes | yes (via MCP callback) |
| `text_to_speech` | yes | yes (via MCP callback) |
| Codex `apply_patch` + sandbox | — | yes (Codex built-in) |
| Codex shell + read/write files | — | yes (Codex built-in) |
| ChatGPT subscription auth | — | yes (via `openai-codex` provider) |
| Native Codex plugins (Linear, GitHub, etc.) | — | yes (auto-migrated) |
| User MCP servers | yes | yes (auto-migrated to codex) |
| Memory + skill review (background) | yes | yes (via item projection) |
| Multi-turn conversations | yes | yes |
| All gateway platforms | yes | yes |
| Non-OpenAI providers | yes | n/a — OpenAI/Codex-scoped |

## Prerequisites

1. **Codex CLI installed:**
   ```bash
   npm i -g @openai/codex
   codex --version   # 0.130.0 or newer
   ```
2. **Codex OAuth login.** The codex subprocess reads `~/.codex/auth.json`. Two ways to populate it:
   ```bash
   codex login                  # writes tokens to ~/.codex/auth.json
   ```
   Hermes' own `hermes auth login codex` writes to `~/.hermes/auth.json` — that's a separate session. **Run `codex login` separately** if you haven't.

3. **(Optional) Install the Codex plugins you want.** When you enable the runtime, Hermes auto-migrates whichever curated plugins you've already installed via Codex CLI:
   ```bash
   codex plugin marketplace add openai-curated
   # then via codex's TUI, install Linear / GitHub / Gmail / etc.
   ```
   Hermes will discover them and write `[plugins."<name>@openai-curated"]` entries to `~/.codex/config.toml` automatically.

## Enabling

In a Hermes session:

```
/codex-runtime codex_app_server
```

That command:
- Verifies the `codex` CLI is installed (blocks with an install hint if not).
- Persists `model.openai_runtime: codex_app_server` to your config.yaml.
- Migrates user MCP servers from `~/.hermes/config.yaml` to `~/.codex/config.toml`.
- **Discovers and migrates installed native Codex plugins** (Linear, GitHub, Gmail, Calendar, Canva, etc.) by querying Codex's `plugin/list` RPC.
- **Registers Hermes' own tools as an MCP server** so the codex subprocess can call back for tools codex doesn't ship with.
- **Writes `default_permissions = ":workspace"`** so the sandbox allows writes within the workspace without prompting for every operation.
- Tells you what was migrated. Takes effect on the **next** session — the current cached agent keeps the prior runtime so prompt caches stay valid.

Synonyms: `/codex-runtime on`, `/codex-runtime off`, `/codex-runtime auto`.

To check current state without changing anything:
```
/codex-runtime
```

You can also set it manually in `~/.hermes/config.yaml`:
```yaml
model:
  openai_runtime: codex_app_server   # default is "auto" (= Hermes runtime)
```

## How approvals work

Codex requests approval before executing commands or applying patches. These get translated into Hermes' standard "Dangerous Command" prompt:

```
╭───────────────────────────────────────╮
│ Dangerous Command                     │
│                                       │
│ /bin/bash -lc 'echo hello > foo.txt'  │
│                                       │
│ ❯ 1. Allow once                       │
│   2. Allow for this session           │
│   3. Deny                             │
│                                       │
│ Codex requests exec in /your/cwd      │
╰───────────────────────────────────────╯
```

- **Allow once** → approve this single command.
- **Allow for this session** → Codex won't re-prompt for similar commands.
- **Deny** → command is rejected; Codex continues in read-only mode.

For `apply_patch` (file edit) approvals, Hermes shows a summary of what changed (`1 add, 1 update: /tmp/new.py, /tmp/old.py`) when codex provides the data via the corresponding `fileChange` item.

## Permission profiles

Codex has three built-in permission profiles:
- `:read-only` — no writes; every shell command requires approval
- `:workspace` — writes within the current workspace allowed without prompts (Hermes' default when you enable the runtime)
- `:danger-no-sandbox` — no sandbox at all (don't use this unless you understand it)

You can override the default in `~/.codex/config.toml` outside Hermes' managed block:

```toml
default_permissions = ":read-only"
```

(Hermes will preserve your override on re-migration as long as it lives outside the `# managed by hermes-agent` markers.)

## Editing `~/.codex/config.toml` safely

Hermes wraps everything it manages between two marker comments:

```toml
# managed by hermes-agent — `hermes codex-runtime migrate` regenerates this section
default_permissions = ":workspace"
[mcp_servers.filesystem]
...
[plugins."github@openai-curated"]
...
# end hermes-agent managed section
```

Anything **outside** that block is yours. Re-running migration (via `/codex-runtime codex_app_server` or whenever you toggle the runtime on) replaces the managed block in place but preserves user content above and below it verbatim. This means you can:

- Add your own MCP servers Hermes doesn't know about
- Override `default_permissions` to `:read-only` if you prefer to be prompted
- Configure codex-only options (model, providers, otel, etc.)
- Add user-defined permission profiles in `[permissions.<name>]` tables

Anything you add **inside** the managed block will get clobbered on the next migration. If you need a tweak that requires editing the managed block, file an issue and we'll add the knob.

## MCP server migration

Hermes' `mcp_servers` config is auto-translated to the TOML format Codex expects. The migration runs every time you enable the runtime and is idempotent — re-runs replace the managed section but preserve any user-edited Codex config.

What translates:

| Hermes (`config.yaml`) | Codex (`config.toml`) |
|---|---|
| `command` + `args` + `env` | stdio transport |
| `url` + `headers` | streamable_http transport |
| `timeout` | `tool_timeout_sec` |
| `connect_timeout` | `startup_timeout_sec` |
| `enabled: false` | `enabled = false` |

What's not migrated:
- Hermes-specific keys like `sampling` (Codex's MCP client has no equivalent — these are dropped with a per-server warning).

## Native Codex plugin migration

Plugins installed via `codex plugin` (Linear, GitHub, Gmail, Calendar, Canva, etc.) are discovered through Codex's `plugin/list` RPC. For each plugin where `installed: true`, Hermes writes a `[plugins."<name>@openai-curated"]` block enabling it in your Hermes session.

This means: when your friend says "I have Calendar and GitHub set up in my Codex CLI" and they enable Hermes' codex runtime, Hermes activates those automatically. No re-configuration needed.

What's NOT migrated:
- Plugins not yet installed in Codex CLI. Install them via `codex plugin` first.
- ChatGPT app marketplace entries (the per-account `app/list` results — these are already enabled inside codex by virtue of your account auth).
- Plugin OAuth — you authorize each plugin once in Codex itself; Hermes doesn't touch credentials.

## Hermes tool callback (the new MCP server)

Codex's built-in toolset covers shell/file ops/patches but doesn't have web search, browser automation, vision, image generation, etc. To keep those usable in a codex turn, Hermes registers itself as an MCP server in `~/.codex/config.toml`:

```toml
[mcp_servers.hermes-tools]
command = "/path/to/python"
args = ["-m", "agent.transports.hermes_tools_mcp_server"]
env = { HERMES_HOME = "/your/.hermes", PYTHONPATH = "...", HERMES_QUIET = "1" }
startup_timeout_sec = 30.0
tool_timeout_sec = 600.0
```

When the model calls `web_search` (or another exposed Hermes tool), codex spawns the `hermes_tools_mcp_server` subprocess via stdio, the request is dispatched through `model_tools.handle_function_call()`, and the result is projected back to codex like any other MCP response.

**Tools available via the callback:** `web_search`, `web_extract`, `browser_navigate`, `browser_click`, `browser_type`, `browser_press`, `browser_snapshot`, `browser_scroll`, `browser_back`, `browser_get_images`, `browser_console`, `browser_vision`, `vision_analyze`, `image_generate`, `skill_view`, `skills_list`, `text_to_speech`.

**Tools NOT available:** `delegate_task`, `memory`, `session_search`, `todo`. These need the running AIAgent context to dispatch (mid-loop state) and a stateless MCP callback can't drive them. Use the default Hermes runtime (`/codex-runtime auto`) when you need these.

## Disabling

Switch back at any time:

```
/codex-runtime auto
```

Effective on the next session. The Codex managed block stays in `~/.codex/config.toml` so you can re-enable later without losing config — or remove it manually if you prefer.

## Limitations

This runtime is **opt-in beta**. Working as of Hermes Agent 2026.5 + Codex CLI 0.130.0:

- Multi-turn conversations
- `commandExecution` and `fileChange` (apply_patch) approvals via Hermes UI
- MCP tool calls (verified against `@modelcontextprotocol/server-filesystem` and the new `hermes-tools` callback)
- Native Codex plugin migration (verified against Linear / GitHub / Calendar inventory)
- Deny/cancel paths
- Toggle on/off cycle
- Memory and skill nudge counters (verified live via integration tests)
- Hermes web_search through codex (verified live: "OpenAI Codex CLI – Getting Started" returned end-to-end)

Known limitations:

- **Hermes auth and codex auth are separate sessions.** You need both `codex login` AND `hermes auth login codex` for the cleanest UX (the runtime uses codex's session for the LLM call). This is a deliberate design choice in Hermes' `_import_codex_cli_tokens` — Hermes won't share OAuth state with codex CLI to avoid clobbering each other on token refresh.
- **`delegate_task`, `memory`, `session_search`, `todo` are unavailable on this runtime.** They need the running AIAgent context which a stateless MCP callback can't provide. Use `/codex-runtime auto` when you need these.
- **No inline patch preview in approval prompts when codex doesn't track the changeset.** Codex's `fileChange` approval params don't always carry the changeset. Hermes caches the data from the corresponding `item/started` notification when possible, but if approval arrives before the item has streamed, the prompt falls back to whatever `reason` codex provides.
- **Sub-second cancellation isn't guaranteed.** Mid-stream interrupts (Ctrl+C while codex is responding) are sent via `turn/interrupt`, but if codex has already flushed the final message, you get the response anyway.

If you find a bug, [open an issue](https://github.com/NousResearch/hermes-agent/issues) with the output of `hermes logs --since 5m`. Mention `codex-runtime` in the title so it's easy to triage.

## Architecture

```
                ┌─── Hermes shell (CLI / TUI / gateway) ───┐
                │  sessions DB · slash commands · memory   │
                │  & skill review · cron · session pickers │
                └──┬──────────────────────────────────────┬┘
                   │ user_message               final     │
                   ▼                            text +    │
        ┌──────────────────────────────────┐   projected  │
        │  AIAgent.run_conversation()       │   messages   │
        │   if api_mode == codex_app_server │              │
        │     → CodexAppServerSession       │              │
        │   else: chat_completions / codex_responses (default)
        └────┬─────────────────────────────┘              │
             │ JSON-RPC over stdio                        │
             ▼                                            │
        ┌──────────────────────────────────┐              │
        │  codex app-server (subprocess)    │──────────────┘
        │   thread/start, turn/start        │
        │   item/* notifications            │
        │   apply_patch + shell + sandbox   │
        │   ┌─────────────────────────┐     │
        │   │  MCP client             │     │
        │   │  ├─ user MCP servers    │     │
        │   │  ├─ native plugins      │     │
        │   │  │   (linear, github,   │     │
        │   │  │    gmail, calendar,  │     │
        │   │  │    canva, ...)       │     │
        │   │  └─ hermes-tools ───────┼─────────────────┐
        │   │       (callback to     │     │           │
        │   │        Hermes' richer  │     │           │
        │   │        tools)          │     │           │
        │   └─────────────────────────┘     │           │
        └──────────────────────────────────┘           │
                                                        │
                                                        ▼
        ┌──────────────────────────────────────────────────────────┐
        │  hermes_tools_mcp_server.py (subprocess on demand)        │
        │   web_search, web_extract, browser_*, vision_analyze,    │
        │   image_generate, skill_view, skills_list, text_to_speech│
        └──────────────────────────────────────────────────────────┘
```

For implementation details, see [PR #24182](https://github.com/NousResearch/hermes-agent/pull/24182) and the [Codex app-server protocol README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md).
