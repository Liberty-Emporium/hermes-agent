"""Tests for the codex MCP plugin migration helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.codex_runtime_plugin_migration import (
    MIGRATION_MARKER,
    MigrationReport,
    _format_toml_value,
    _strip_existing_managed_block,
    _translate_one_server,
    migrate,
    render_codex_toml_section,
)


# ---- per-server translation ----

class TestTranslateOneServer:
    def test_stdio_basic(self):
        cfg, skipped = _translate_one_server("filesystem", {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"FOO": "bar"},
        })
        assert cfg == {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"FOO": "bar"},
        }
        assert skipped == []

    def test_stdio_with_cwd(self):
        cfg, _ = _translate_one_server("custom", {
            "command": "/usr/bin/myserver",
            "cwd": "/var/lib/mcp",
        })
        assert cfg["cwd"] == "/var/lib/mcp"

    def test_http_basic(self):
        cfg, skipped = _translate_one_server("api", {
            "url": "https://x.example/mcp",
            "headers": {"Authorization": "Bearer abc"},
        })
        assert cfg == {
            "url": "https://x.example/mcp",
            "http_headers": {"Authorization": "Bearer abc"},
        }
        assert skipped == []

    def test_sse_falls_under_streamable_http_with_warning(self):
        cfg, skipped = _translate_one_server("sse_server", {
            "url": "http://localhost:8000/sse",
            "transport": "sse",
        })
        assert cfg["url"] == "http://localhost:8000/sse"
        assert any("sse" in s.lower() for s in skipped)

    def test_timeouts_translate(self):
        cfg, _ = _translate_one_server("x", {
            "command": "y",
            "timeout": 180,
            "connect_timeout": 30,
        })
        assert cfg["tool_timeout_sec"] == 180.0
        assert cfg["startup_timeout_sec"] == 30.0

    def test_non_numeric_timeout_skipped(self):
        cfg, skipped = _translate_one_server("x", {
            "command": "y",
            "timeout": "not-a-number",
        })
        assert "tool_timeout_sec" not in cfg
        assert any("timeout" in s and "numeric" in s for s in skipped)

    def test_disabled_server_emits_enabled_false(self):
        cfg, _ = _translate_one_server("x", {
            "command": "y",
            "enabled": False,
        })
        assert cfg["enabled"] is False

    def test_enabled_true_omitted(self):
        cfg, _ = _translate_one_server("x", {"command": "y", "enabled": True})
        assert "enabled" not in cfg  # codex defaults to true

    def test_command_and_url_prefers_stdio_warns(self):
        cfg, skipped = _translate_one_server("x", {
            "command": "y", "url": "http://z",
        })
        assert "command" in cfg
        assert "url" not in cfg
        assert any("url" in s for s in skipped)

    def test_no_transport_returns_none(self):
        cfg, skipped = _translate_one_server("broken", {"description": "x"})
        assert cfg is None
        assert "no command or url" in skipped[0]

    def test_sampling_dropped_with_warning(self):
        cfg, skipped = _translate_one_server("x", {
            "command": "y",
            "sampling": {"enabled": True, "model": "gemini-3-flash"},
        })
        assert "sampling" not in cfg
        assert any("sampling" in s for s in skipped)

    def test_unknown_keys_warned(self):
        cfg, skipped = _translate_one_server("x", {
            "command": "y",
            "totally_made_up_key": "value",
        })
        assert "totally_made_up_key" not in cfg
        assert any("totally_made_up_key" in s for s in skipped)

    def test_non_dict_input(self):
        cfg, skipped = _translate_one_server("x", "notadict")  # type: ignore[arg-type]
        assert cfg is None


# ---- TOML rendering ----

class TestTomlValueFormatter:
    def test_string_quoted(self):
        assert _format_toml_value("hello") == '"hello"'

    def test_string_with_quotes_escaped(self):
        assert _format_toml_value('a"b') == '"a\\"b"'

    def test_bool(self):
        assert _format_toml_value(True) == "true"
        assert _format_toml_value(False) == "false"

    def test_int(self):
        assert _format_toml_value(42) == "42"

    def test_float(self):
        assert _format_toml_value(180.0) == "180.0"

    def test_list_of_strings(self):
        assert _format_toml_value(["a", "b"]) == '["a", "b"]'

    def test_inline_table(self):
        out = _format_toml_value({"FOO": "bar"})
        assert out == '{ FOO = "bar" }'

    def test_empty_inline_table(self):
        assert _format_toml_value({}) == "{}"

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError):
            _format_toml_value(object())


class TestRenderToml:
    def test_starts_with_marker(self):
        out = render_codex_toml_section({})
        assert out.startswith(MIGRATION_MARKER)

    def test_empty_servers_emits_placeholder(self):
        out = render_codex_toml_section({})
        assert "no MCP servers" in out

    def test_servers_sorted_alphabetically(self):
        out = render_codex_toml_section({
            "zoo": {"command": "z"},
            "alpha": {"command": "a"},
            "middle": {"command": "m"},
        })
        # Find the section header positions and confirm order
        a_pos = out.find("[mcp_servers.alpha]")
        m_pos = out.find("[mcp_servers.middle]")
        z_pos = out.find("[mcp_servers.zoo]")
        assert 0 < a_pos < m_pos < z_pos

    def test_server_with_args_and_env(self):
        out = render_codex_toml_section({
            "fs": {
                "command": "npx",
                "args": ["-y", "filesystem"],
                "env": {"PATH": "/usr/bin"},
            }
        })
        assert "[mcp_servers.fs]" in out
        assert 'command = "npx"' in out
        assert 'args = ["-y", "filesystem"]' in out
        # Env emitted as inline table
        assert 'env = { PATH = "/usr/bin" }' in out


# ---- existing-block stripping ----

class TestStripExistingManagedBlock:
    def test_no_managed_block_unchanged(self):
        text = "[other]\nfoo = 1\n"
        assert _strip_existing_managed_block(text) == text

    def test_strips_managed_block_alone(self):
        text = (
            f"{MIGRATION_MARKER}\n"
            "\n"
            "[mcp_servers.fs]\n"
            'command = "npx"\n'
        )
        assert _strip_existing_managed_block(text).strip() == ""

    def test_preserves_user_content_above_managed_block(self):
        text = (
            "[model]\n"
            'name = "gpt-5.5"\n'
            "\n"
            f"{MIGRATION_MARKER}\n"
            "[mcp_servers.fs]\n"
            'command = "x"\n'
        )
        out = _strip_existing_managed_block(text)
        assert "[model]" in out
        assert 'name = "gpt-5.5"' in out
        assert "mcp_servers.fs" not in out

    def test_preserves_unrelated_section_after_managed_block(self):
        text = (
            f"{MIGRATION_MARKER}\n"
            "[mcp_servers.fs]\n"
            'command = "x"\n'
            "\n"
            "[providers]\n"
            'foo = "bar"\n'
        )
        out = _strip_existing_managed_block(text)
        assert "mcp_servers.fs" not in out
        assert "[providers]" in out
        assert 'foo = "bar"' in out


# ---- end-to-end migrate() ----

class TestMigrate:
    def test_no_servers_writes_placeholder(self, tmp_path):
        report = migrate({}, codex_home=tmp_path)
        assert report.written
        text = (tmp_path / "config.toml").read_text()
        assert MIGRATION_MARKER in text
        assert "no MCP servers" in text

    def test_dry_run_doesnt_write(self, tmp_path):
        report = migrate({"mcp_servers": {"x": {"command": "y"}}},
                         codex_home=tmp_path, dry_run=True)
        assert report.dry_run is True
        assert not (tmp_path / "config.toml").exists()
        assert "x" in report.migrated

    def test_full_migration_round_trip(self, tmp_path):
        hermes_cfg = {
            "mcp_servers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                },
                "github": {
                    "url": "https://api.github.com/mcp",
                    "headers": {"Authorization": "Bearer x"},
                },
            }
        }
        report = migrate(hermes_cfg, codex_home=tmp_path)
        assert report.written
        text = (tmp_path / "config.toml").read_text()
        assert "[mcp_servers.filesystem]" in text
        assert "[mcp_servers.github]" in text
        assert 'command = "npx"' in text
        assert 'url = "https://api.github.com/mcp"' in text

    def test_idempotent_re_run_replaces_managed_block(self, tmp_path):
        # First migration
        migrate({"mcp_servers": {"a": {"command": "x"}}}, codex_home=tmp_path)
        first_text = (tmp_path / "config.toml").read_text()
        assert "[mcp_servers.a]" in first_text
        # Second migration with different servers
        migrate({"mcp_servers": {"b": {"command": "y"}}}, codex_home=tmp_path)
        second_text = (tmp_path / "config.toml").read_text()
        assert "[mcp_servers.a]" not in second_text
        assert "[mcp_servers.b]" in second_text

    def test_preserves_user_codex_config_above_marker(self, tmp_path):
        target = tmp_path / "config.toml"
        target.write_text(
            "[model]\n"
            'profile = "default"\n'
            "\n"
            "[providers.openai]\n"
            'api_key = "sk-test"\n'
        )
        migrate({"mcp_servers": {"a": {"command": "x"}}}, codex_home=tmp_path)
        new_text = target.read_text()
        # User's codex config preserved
        assert "[model]" in new_text
        assert 'profile = "default"' in new_text
        assert "[providers.openai]" in new_text
        # And new MCP block appended
        assert "[mcp_servers.a]" in new_text
        assert MIGRATION_MARKER in new_text

    def test_skipped_keys_reported(self, tmp_path):
        report = migrate({
            "mcp_servers": {
                "x": {
                    "command": "y",
                    "sampling": {"enabled": True},  # codex has no equivalent
                }
            }
        }, codex_home=tmp_path)
        assert "x" in report.skipped_keys_per_server
        assert any("sampling" in s for s in report.skipped_keys_per_server["x"])

    def test_invalid_mcp_servers_value(self, tmp_path):
        report = migrate({"mcp_servers": "notadict"}, codex_home=tmp_path)
        assert any("not a dict" in e for e in report.errors)

    def test_server_without_transport_skipped_with_error(self, tmp_path):
        report = migrate({
            "mcp_servers": {"broken": {"description": "no command/url"}}
        }, codex_home=tmp_path)
        assert "broken" not in report.migrated
        assert any("broken" in e for e in report.errors)

    def test_summary_reports_migration_count(self, tmp_path):
        report = migrate({
            "mcp_servers": {"a": {"command": "x"}, "b": {"command": "y"}}
        }, codex_home=tmp_path)
        summary = report.summary()
        assert "Migrated 2 MCP server(s)" in summary
        assert "- a" in summary
        assert "- b" in summary
