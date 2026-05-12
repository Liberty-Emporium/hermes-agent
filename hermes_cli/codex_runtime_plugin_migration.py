"""Migrate Hermes' MCP server config to the format Codex expects.

When the user enables the codex_app_server runtime, the codex subprocess
runs its own MCP client (not Hermes'). For that to be useful, the user's
MCP servers configured in ~/.hermes/config.yaml need to be visible to
codex too. This module reads Hermes' YAML and writes the equivalent
~/.codex/config.toml entries.

What translates:
  Hermes mcp_servers.<name>.command/args/env  → codex stdio transport
  Hermes mcp_servers.<name>.url/headers       → codex streamable_http transport
  Hermes mcp_servers.<name>.timeout           → codex tool_timeout_sec
  Hermes mcp_servers.<name>.connect_timeout   → codex startup_timeout_sec

What does NOT translate (warned + skipped):
  Hermes-specific keys (sampling, etc.) — codex's MCP client has no
  equivalent. Dropped with a per-server warning in the migration report.

What this is NOT:
  This is one-way config translation, not bidirectional sync. If the user
  edits ~/.codex/config.toml afterwards (e.g. adds codex-only servers),
  re-running migration replaces the migrated section but preserves
  unrelated codex config (model, providers, sandbox profiles, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Marker comment at the top of the migrated section so re-runs can detect
# what's ours and what's user-edited.
MIGRATION_MARKER = (
    "# managed by hermes-agent — `hermes codex-runtime migrate` regenerates this section"
)


@dataclass
class MigrationReport:
    """Outcome of a migration pass."""

    target_path: Optional[Path] = None
    migrated: list[str] = field(default_factory=list)
    skipped_keys_per_server: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    written: bool = False
    dry_run: bool = False

    def summary(self) -> str:
        lines = []
        if self.dry_run:
            lines.append(f"(dry run) Would write {self.target_path}")
        elif self.written:
            lines.append(f"Wrote {self.target_path}")
        if self.migrated:
            lines.append(f"Migrated {len(self.migrated)} MCP server(s):")
            for name in self.migrated:
                skipped = self.skipped_keys_per_server.get(name, [])
                note = (
                    f" (skipped: {', '.join(skipped)})" if skipped else ""
                )
                lines.append(f"  - {name}{note}")
        else:
            lines.append("No MCP servers found in Hermes config.")
        for err in self.errors:
            lines.append(f"⚠ {err}")
        return "\n".join(lines)


# Hermes keys that codex's MCP schema doesn't support — dropped during
# migration with a warning. Anything not on the keep list AND not the
# transport keys is added to skipped.
_KNOWN_HERMES_KEYS = {
    # transport — stdio
    "command", "args", "env", "cwd",
    # transport — http
    "url", "headers", "transport",
    # timeouts
    "timeout", "connect_timeout",
    # general
    "enabled", "description",
}

# Subset that have a direct codex equivalent.
_KEYS_DROPPED_WITH_WARNING = {
    # Hermes' sampling subsection — codex MCP has no equivalent
    "sampling",
}


def _translate_one_server(
    name: str, hermes_cfg: dict
) -> tuple[Optional[dict], list[str]]:
    """Translate one Hermes MCP server config to the codex inline-table dict
    representation. Returns (codex_entry, skipped_keys).

    codex_entry is a dict ready for TOML serialization, or None when the
    server can't be translated (e.g. neither command nor url present)."""
    if not isinstance(hermes_cfg, dict):
        return None, []

    skipped: list[str] = []
    out: dict[str, Any] = {}

    has_command = bool(hermes_cfg.get("command"))
    has_url = bool(hermes_cfg.get("url"))

    if has_command and has_url:
        skipped.append("url (both command and url set; preferring stdio)")
        has_url = False

    if has_command:
        # Stdio transport
        out["command"] = str(hermes_cfg["command"])
        args = hermes_cfg.get("args") or []
        if args:
            out["args"] = [str(a) for a in args]
        env = hermes_cfg.get("env") or {}
        if env:
            # Codex expects string values
            out["env"] = {str(k): str(v) for k, v in env.items()}
        cwd = hermes_cfg.get("cwd")
        if cwd:
            out["cwd"] = str(cwd)
    elif has_url:
        # streamable_http transport (codex covers both http and SSE here)
        out["url"] = str(hermes_cfg["url"])
        headers = hermes_cfg.get("headers") or {}
        if headers:
            out["http_headers"] = {str(k): str(v) for k, v in headers.items()}
        # Hermes' transport: sse hint is informational; codex auto-negotiates
        if hermes_cfg.get("transport") == "sse":
            skipped.append("transport=sse (codex auto-negotiates)")
    else:
        return None, ["no command or url field"]

    # Timeouts
    if "timeout" in hermes_cfg:
        try:
            out["tool_timeout_sec"] = float(hermes_cfg["timeout"])
        except (TypeError, ValueError):
            skipped.append("timeout (not numeric)")
    if "connect_timeout" in hermes_cfg:
        try:
            out["startup_timeout_sec"] = float(hermes_cfg["connect_timeout"])
        except (TypeError, ValueError):
            skipped.append("connect_timeout (not numeric)")

    # Enabled flag (codex defaults to true so we only emit when explicitly false)
    if hermes_cfg.get("enabled") is False:
        out["enabled"] = False

    # Detect keys we explicitly drop with warning
    for key in hermes_cfg:
        if key in _KEYS_DROPPED_WITH_WARNING:
            skipped.append(f"{key} (no codex equivalent)")
        elif key not in _KNOWN_HERMES_KEYS:
            skipped.append(f"{key} (unknown Hermes key)")

    return out, skipped


def _format_toml_value(value: Any) -> str:
    """Minimal TOML value formatter for the value types we emit.

    We only emit strings, numbers, booleans, and tables of those — no nested
    arrays of tables. This covers everything codex's MCP schema accepts."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # Use double-quoted TOML string with backslash escaping
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        items = ", ".join(_format_toml_value(v) for v in value)
        return f"[{items}]"
    if isinstance(value, dict):
        items = ", ".join(
            f'{_quote_key(k)} = {_format_toml_value(v)}' for k, v in value.items()
        )
        return "{ " + items + " }" if items else "{}"
    raise ValueError(f"Unsupported TOML value type: {type(value).__name__}")


def _quote_key(key: str) -> str:
    """Return key bare-or-quoted depending on whether it's a valid bare key."""
    if all(c.isalnum() or c in "-_" for c in key) and key:
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_codex_toml_section(servers: dict[str, dict]) -> str:
    """Render an [mcp_servers.<name>] section block for the codex config.toml."""
    out = [MIGRATION_MARKER]
    if not servers:
        out.append("# (no MCP servers configured in Hermes)")
        return "\n".join(out) + "\n"
    for name in sorted(servers.keys()):
        cfg = servers[name]
        out.append("")
        out.append(f"[mcp_servers.{_quote_key(name)}]")
        for k, v in cfg.items():
            out.append(f"{_quote_key(k)} = {_format_toml_value(v)}")
    return "\n".join(out) + "\n"


def _strip_existing_managed_block(toml_text: str) -> str:
    """Remove any prior managed section so re-runs idempotently replace it.

    The managed section is everything between MIGRATION_MARKER and the next
    section header that is NOT [mcp_servers.*] OR end-of-file."""
    lines = toml_text.splitlines(keepends=True)
    out: list[str] = []
    in_managed = False
    for line in lines:
        if line.rstrip("\n") == MIGRATION_MARKER:
            in_managed = True
            continue
        if in_managed:
            stripped = line.lstrip()
            # Hand back control once we hit a non-mcp section.
            if stripped.startswith("[") and not stripped.startswith("[mcp_servers"):
                in_managed = False
                out.append(line)
            # Otherwise swallow the line (it's part of the old managed block).
            continue
        out.append(line)
    return "".join(out)


def migrate(
    hermes_config: dict,
    *,
    codex_home: Optional[Path] = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Translate Hermes mcp_servers config into ~/.codex/config.toml.

    Args:
        hermes_config: full ~/.hermes/config.yaml dict
        codex_home: override CODEX_HOME (defaults to ~/.codex)
        dry_run: skip the actual write; report what would happen
    """
    report = MigrationReport(dry_run=dry_run)
    codex_home = codex_home or Path.home() / ".codex"
    target = codex_home / "config.toml"
    report.target_path = target

    hermes_servers = (hermes_config or {}).get("mcp_servers") or {}
    if not isinstance(hermes_servers, dict):
        report.errors.append(
            "mcp_servers in Hermes config is not a dict; cannot migrate."
        )
        return report

    translated: dict[str, dict] = {}
    for name, cfg in hermes_servers.items():
        out, skipped = _translate_one_server(str(name), cfg or {})
        if out is None:
            report.errors.append(
                f"server {name!r} skipped: {', '.join(skipped) or 'no transport configured'}"
            )
            continue
        translated[str(name)] = out
        if skipped:
            report.skipped_keys_per_server[str(name)] = skipped
        report.migrated.append(str(name))

    # Build the new managed block
    managed_block = render_codex_toml_section(translated)

    # Read existing codex config if any, strip the prior managed block,
    # append the new one.
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except Exception as exc:
            report.errors.append(f"could not read {target}: {exc}")
            return report
        without_managed = _strip_existing_managed_block(existing)
        # Ensure exactly one blank line between user content and managed block
        if without_managed and not without_managed.endswith("\n"):
            without_managed += "\n"
        new_text = (
            without_managed.rstrip("\n") + "\n\n" + managed_block
            if without_managed.strip()
            else managed_block
        )
    else:
        new_text = managed_block

    if dry_run:
        return report

    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        target.write_text(new_text, encoding="utf-8")
        report.written = True
    except Exception as exc:
        report.errors.append(f"could not write {target}: {exc}")
    return report
