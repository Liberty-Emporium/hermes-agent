---
title: Codex App-Server Runtime (optional)
sidebar_label: Codex App-Server Runtime
---

# Codex App-Server Runtime

Hermes can optionally hand `openai/*` and `openai-codex/*` turns to the [Codex CLI app-server](https://github.com/openai/codex) instead of running its own tool loop. When enabled, terminal commands, file edits, sandboxing, and MCP tool calls all execute inside Codex's runtime — Hermes becomes the shell around it (sessions DB, slash commands, gateway, memory and skill review).

This is **opt-in only**. Default Hermes behavior is unchanged unless you flip the flag.

## Why

- Run OpenAI agent turns against your **ChatGPT subscription** (no API key required) using the same auth flow Codex CLI uses.
- Use **Codex's own toolset and sandbox** — `apply_patch` with Codex's diff format, seatbelt/landlock sandboxing, native shell execution, Codex's MCP client.
- **Memory and skill nudges keep working** — Codex's events are projected into Hermes' message shape so the self-improvement loop sees a normal-looking transcript.

## Trade-offs

|  | Hermes default runtime | Codex app-server (opt-in) |
|---|---|---|
| `delegate_task` subagents | available | unavailable on this runtime |
| Hermes browser / web / kanban tools | available | replaced by Codex's toolset |
| Codex `apply_patch` + sandbox | — | yes |
| ChatGPT subscription auth | — | yes (via `openai-codex` provider) |
| MCP servers | served by Hermes | migrated to Codex's MCP client |
| Memory + skill review | yes | yes (via item projection) |
| Multi-turn conversations | yes | yes |
| All gateway platforms | yes | yes |
| Non-OpenAI providers | yes | n/a — OpenAI/Codex-scoped |

## Prerequisites

1. **Codex CLI installed:**
   ```bash
   npm i -g @openai/codex
   codex --version   # 0.130.0 or newer
   ```
2. **OAuth login.** Either log in via Hermes or via Codex CLI directly:
   ```bash
   hermes auth login codex      # writes tokens to ~/.hermes/auth.json
   # or
   codex login                  # writes tokens to ~/.codex/auth.json
   ```
   Both paths work — Hermes will use whichever credential is available.

## Enabling

In a Hermes session:

```
/codex-runtime codex_app_server
```

That command:
- Verifies the `codex` CLI is installed (blocks with an install hint if not).
- Persists `model.openai_runtime: codex_app_server` to your config.yaml.
- Migrates any MCP servers in `~/.hermes/config.yaml` to `~/.codex/config.toml` so Codex's own MCP client picks them up.
- Tells you that `delegate_task` is unavailable on this runtime.
- Note: takes effect on the **next** session — the current cached agent keeps the prior runtime so prompt caches stay valid.

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

The same flow applies to file edits (`apply_patch`).

## Permission profiles

Codex has its own permission/sandbox model (read-only, workspace-write, full-access). Hermes does NOT override Codex's profile selection — Codex uses the default from `~/.codex/config.toml`. If you want a write-capable default profile, configure it there the standard Codex way:

```toml
# ~/.codex/config.toml
[permissions]
default = "workspace-write"
```

Without this, Codex defaults to read-only and prompts for every write attempt — which is why you'll see frequent approval prompts on a fresh setup.

## MCP server migration

Hermes' `mcp_servers` config is auto-translated to the TOML format Codex expects. The migration runs every time you enable the runtime and is idempotent — re-running replaces the managed section but preserves any user-edited Codex config.

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
- Skills are NOT migrated (Codex picks `AGENTS.md` natively from cwd; no translation needed).

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
- MCP tool calls (verified against `@modelcontextprotocol/server-filesystem`)
- Deny/cancel paths
- Toggle on/off cycle
- Memory and skill nudge counters (verified live via integration tests)

Known limitations:

- **Subagents disabled.** `delegate_task` doesn't work inside a Codex turn — Codex builds its own tool list and Hermes can't inject into it. Use `/codex-runtime auto` if you need subagents on the same provider.
- **Hermes browser/web/kanban tools unavailable on this runtime.** Codex doesn't expose them; use Codex's MCP equivalents or fall back to the default runtime.
- **Permission profile selection on `thread/start` is gated behind Codex's experimental API.** Hermes uses Codex's own default — configure your preferred profile in `~/.codex/config.toml`.
- **No inline patch preview in approval prompts.** Codex's `fileChange` approval params don't carry the changeset. Future Codex versions may include it.

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
        │   MCP client                      │
        └──────────────────────────────────┘
```

For implementation details, see [PR #24182](https://github.com/NousResearch/hermes-agent/pull/24182) and the [Codex app-server protocol README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md).
