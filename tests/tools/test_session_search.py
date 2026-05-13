"""Tests for tools/session_search_tool.py — helper functions and search dispatcher."""

import asyncio
import json
import time
import pytest

from tools.session_search_tool import (
    _format_timestamp,
    _format_conversation,
    _truncate_around_matches,
    _get_session_search_max_concurrency,
    _list_recent_sessions,
    _HIDDEN_SESSION_SOURCES,
    MAX_SESSION_CHARS,
    SESSION_SEARCH_SCHEMA,
)


# =========================================================================
# Tool schema guidance
# =========================================================================

class TestHiddenSessionSources:
    """Verify the _HIDDEN_SESSION_SOURCES constant used for third-party isolation."""

    def test_tool_source_is_hidden(self):
        assert "tool" in _HIDDEN_SESSION_SOURCES

    def test_standard_sources_not_hidden(self):
        for src in ("cli", "telegram", "discord", "slack", "cron"):
            assert src not in _HIDDEN_SESSION_SOURCES


class TestSessionSearchSchema:
    def test_keeps_cross_session_recall_guidance_without_current_session_nudge(self):
        description = SESSION_SEARCH_SCHEMA["description"]
        assert "past conversations" in description
        assert "recent turns of the current session" not in description


# =========================================================================
# _format_timestamp
# =========================================================================

class TestFormatTimestamp:
    def test_unix_float(self):
        ts = 1700000000.0  # Nov 14, 2023
        result = _format_timestamp(ts)
        assert "2023" in result or "November" in result

    def test_unix_int(self):
        result = _format_timestamp(1700000000)
        assert isinstance(result, str)
        assert len(result) > 5

    def test_iso_string(self):
        result = _format_timestamp("2024-01-15T10:30:00")
        assert isinstance(result, str)

    def test_none_returns_unknown(self):
        assert _format_timestamp(None) == "unknown"

    def test_numeric_string(self):
        result = _format_timestamp("1700000000.0")
        assert isinstance(result, str)
        assert "unknown" not in result.lower()


# =========================================================================
# _format_conversation
# =========================================================================

class TestFormatConversation:
    def test_basic_messages(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = _format_conversation(msgs)
        assert "[USER]: Hello" in result
        assert "[ASSISTANT]: Hi there!" in result

    def test_tool_message(self):
        msgs = [
            {"role": "tool", "content": "search results", "tool_name": "web_search"},
        ]
        result = _format_conversation(msgs)
        assert "[TOOL:web_search]" in result

    def test_long_tool_output_truncated(self):
        msgs = [
            {"role": "tool", "content": "x" * 1000, "tool_name": "terminal"},
        ]
        result = _format_conversation(msgs)
        assert "[truncated]" in result

    def test_assistant_with_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search"}},
                    {"function": {"name": "terminal"}},
                ],
            },
        ]
        result = _format_conversation(msgs)
        assert "web_search" in result
        assert "terminal" in result

    def test_empty_messages(self):
        result = _format_conversation([])
        assert result == ""


# =========================================================================
# _truncate_around_matches
# =========================================================================

class TestTruncateAroundMatches:
    def test_short_text_unchanged(self):
        text = "Short text about docker"
        result = _truncate_around_matches(text, "docker")
        assert result == text

    def test_long_text_truncated(self):
        # Create text longer than MAX_SESSION_CHARS with query term in middle
        padding = "x" * (MAX_SESSION_CHARS + 5000)
        text = padding + " KEYWORD_HERE " + padding
        result = _truncate_around_matches(text, "KEYWORD_HERE")
        assert len(result) <= MAX_SESSION_CHARS + 100  # +100 for prefix/suffix markers
        assert "KEYWORD_HERE" in result

    def test_truncation_adds_markers(self):
        text = "a" * 50000 + " target " + "b" * (MAX_SESSION_CHARS + 5000)
        result = _truncate_around_matches(text, "target")
        assert "truncated" in result.lower()

    def test_no_match_takes_from_start(self):
        text = "x" * (MAX_SESSION_CHARS + 5000)
        result = _truncate_around_matches(text, "nonexistent")
        # Should take from the beginning
        assert result.startswith("x")

    def test_match_at_beginning(self):
        text = "KEYWORD " + "x" * (MAX_SESSION_CHARS + 5000)
        result = _truncate_around_matches(text, "KEYWORD")
        assert "KEYWORD" in result

    def test_multiword_phrase_match_beats_individual_term(self):
        """Full phrase deep in text should be found even when a single term
        appears much earlier in boilerplate."""
        boilerplate = "The project setup is complex. " * 500  # ~15K, has 'project' early
        filler = "x" * (MAX_SESSION_CHARS + 20000)
        target = "We reviewed the keystone project roadmap in detail."
        text = boilerplate + filler + target + filler
        result = _truncate_around_matches(text, "keystone project")
        assert "keystone project" in result.lower()

    def test_multiword_proximity_cooccurrence(self):
        """When exact phrase is absent, terms co-occurring within proximity
        should be preferred over a lone early term."""
        early = "project " + "a" * (MAX_SESSION_CHARS + 20000)
        # Place 'keystone' and 'project' near each other (but not as exact phrase)
        cooccur = "this keystone initiative for the project was pivotal"
        tail = "b" * (MAX_SESSION_CHARS + 20000)
        text = early + cooccur + tail
        result = _truncate_around_matches(text, "keystone project")
        assert "keystone" in result.lower()
        assert "project" in result.lower()

    def test_multiword_window_maximises_coverage(self):
        """Sliding window should capture as many match clusters as possible."""
        # Place two phrase matches: one at ~50K, one at ~60K, both should fit
        pre = "z" * 50000
        match1 = " alpha beta "
        gap = "z" * 10000
        match2 = " alpha beta "
        post = "z" * (MAX_SESSION_CHARS + 40000)
        text = pre + match1 + gap + match2 + post
        result = _truncate_around_matches(text, "alpha beta")
        assert result.lower().count("alpha beta") == 2


class TestSessionSearchConcurrency:
    def test_defaults_to_three(self):
        assert _get_session_search_max_concurrency() == 3

    def test_reads_and_clamps_configured_value(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"auxiliary": {"session_search": {"max_concurrency": 9}}},
        )
        assert _get_session_search_max_concurrency() == 5

    def test_session_search_respects_configured_concurrency_limit(self, monkeypatch):
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"auxiliary": {"session_search": {"max_concurrency": 1}}},
        )

        max_seen = {"value": 0}
        active = {"value": 0}

        async def fake_summarize(_text, _query, _meta):
            active["value"] += 1
            max_seen["value"] = max(max_seen["value"], active["value"])
            await asyncio.sleep(0.01)
            active["value"] -= 1
            return "summary"

        monkeypatch.setattr("tools.session_search_tool._summarize_session", fake_summarize)
        monkeypatch.setattr("model_tools._run_async", lambda coro: asyncio.run(coro))

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "s1", "source": "cli", "session_started": 1709500000, "model": "test"},
            {"session_id": "s2", "source": "cli", "session_started": 1709500001, "model": "test"},
            {"session_id": "s3", "source": "cli", "session_started": 1709500002, "model": "test"},
        ]
        mock_db.get_session.side_effect = lambda sid: {
            "id": sid,
            "parent_session_id": None,
            "source": "cli",
            "started_at": 1709500000,
        }
        mock_db.get_messages_as_conversation.side_effect = lambda sid: [
            {"role": "user", "content": f"message from {sid}"},
            {"role": "assistant", "content": "response"},
        ]

        result = json.loads(session_search(query="message", db=mock_db, limit=3, mode="summary"))

        assert result["success"] is True
        assert result["mode"] == "summary"
        assert result["count"] == 3
        assert max_seen["value"] == 1


class TestRecentSessionListing:
    def test_recent_mode_requests_last_active_ordering(self):
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.list_sessions_rich.return_value = []

        result = json.loads(_list_recent_sessions(mock_db, limit=5))

        assert result["success"] is True
        mock_db.list_sessions_rich.assert_called_once_with(
            limit=10,
            exclude_sources=["tool"],
            order_by_last_active=True,
        )

    def test_current_child_session_excludes_root_lineage_even_when_child_id_is_longer(self):
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.list_sessions_rich.return_value = [
            {
                "id": "root",
                "title": "Current conversation",
                "source": "cli",
                "started_at": 1709500000,
                "last_active": 1709500100,
                "message_count": 4,
                "preview": "current root",
                "parent_session_id": None,
            },
            {
                "id": "other_session",
                "title": "Other conversation",
                "source": "cli",
                "started_at": 1709400000,
                "last_active": 1709400100,
                "message_count": 3,
                "preview": "other root",
                "parent_session_id": None,
            },
        ]

        def _get_session(session_id):
            if session_id == "child_session_id_that_is_definitely_longer":
                return {"parent_session_id": "root"}
            if session_id == "root":
                return {"parent_session_id": None}
            return None

        mock_db.get_session.side_effect = _get_session

        result = json.loads(_list_recent_sessions(
            mock_db,
            limit=5,
            current_session_id="child_session_id_that_is_definitely_longer",
        ))

        assert result["success"] is True
        assert [item["session_id"] for item in result["results"]] == ["other_session"]
        assert all(item["session_id"] != "root" for item in result["results"])


# =========================================================================
# session_search (dispatcher)
# =========================================================================

class TestSessionSearch:
    def test_no_db_lazily_opens_default_session_db(self, monkeypatch):
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = []

        class FakeSessionDB:
            def __new__(cls):
                return mock_db

        import types
        import sys

        fake_state = types.ModuleType("hermes_state")
        fake_state.SessionDB = FakeSessionDB
        monkeypatch.setitem(sys.modules, "hermes_state", fake_state)

        result = json.loads(session_search(query="test"))
        assert result["success"] is True
        mock_db.search_messages.assert_called_once()

    def test_empty_query_returns_error(self):
        from tools.session_search_tool import session_search
        mock_db = object()
        result = json.loads(session_search(query="", db=mock_db))
        assert result["success"] is False

    def test_whitespace_query_returns_error(self):
        from tools.session_search_tool import session_search
        mock_db = object()
        result = json.loads(session_search(query="   ", db=mock_db))
        assert result["success"] is False

    def test_current_session_excluded(self):
        """session_search should never return the current session."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        current_sid = "20260304_120000_abc123"

        # Simulate FTS5 returning matches only from the current session
        mock_db.search_messages.return_value = [
            {"session_id": current_sid, "content": "test match", "source": "cli",
             "session_started": 1709500000, "model": "test"},
        ]
        mock_db.get_session.return_value = {"parent_session_id": None}

        result = json.loads(session_search(
            query="test", db=mock_db, current_session_id=current_sid,
        ))
        assert result["success"] is True
        assert result["count"] == 0
        assert result["results"] == []

    def test_current_session_excluded_keeps_others(self):
        """Other sessions should still be returned when current is excluded."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        current_sid = "20260304_120000_abc123"
        other_sid = "20260303_100000_def456"

        mock_db.search_messages.return_value = [
            {"session_id": current_sid, "content": "match 1", "source": "cli",
             "session_started": 1709500000, "model": "test"},
            {"session_id": other_sid, "content": "match 2", "source": "telegram",
             "session_started": 1709400000, "model": "test"},
        ]
        mock_db.get_session.return_value = {"parent_session_id": None}
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        # Mock async_call_llm to raise RuntimeError → summarizer returns None
        from unittest.mock import AsyncMock, patch as _patch
        with _patch("tools.session_search_tool.async_call_llm",
                     new_callable=AsyncMock,
                     side_effect=RuntimeError("no provider")):
            result = json.loads(session_search(
                query="test", db=mock_db, current_session_id=current_sid,
            ))

        assert result["success"] is True
        # Current session should be skipped, only other_sid should appear
        assert result["sessions_searched"] == 1
        assert current_sid not in [r.get("session_id") for r in result.get("results", [])]

    def test_default_search_returns_summary_mode_recap(self, monkeypatch):
        """Default keyword search should run the LLM summariser path (the recall users want)."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        async def fake_summarize(text, query, meta):
            assert "full transcript about session_search" in text
            assert query == "session_search"
            return "focused default summary"

        monkeypatch.setattr("tools.session_search_tool._summarize_session", fake_summarize)
        monkeypatch.setattr("model_tools._run_async", lambda coro: asyncio.run(coro))

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {
                "id": 123,
                "session_id": "other_sid",
                "role": "user",
                "snippet": "we discussed >>>session_search<<< latency",
                "context": [
                    {"role": "user", "content": "session_search is slow"},
                    {"role": "assistant", "content": "the LLM summary is the bottleneck"},
                ],
                "source": "cli",
                "session_started": 1709400000,
                "model": "test-model",
            },
        ]
        mock_db.get_session.return_value = {"parent_session_id": None, "title": "Latency debug", "source": "cli", "started_at": 1709400000}
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "full transcript about session_search"},
        ]

        result = json.loads(session_search(query="session_search", db=mock_db))

        assert result["success"] is True
        assert result["mode"] == "summary"
        assert result["count"] == 1
        entry = result["results"][0]
        assert entry["summary"] == "focused default summary"
        assert entry["model"] == "test-model"
        # Summary mode does NOT include snippet/context fields — only the metadata + summary
        assert "snippet" not in entry
        assert "context" not in entry
        mock_db.get_messages_as_conversation.assert_called_once_with("other_sid")

    def test_explicit_fast_mode_returns_snippets_without_llm_or_full_session_load(self, monkeypatch):
        """mode='fast' stays on the DB/snippet path and avoids LLM latency."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        async def fail_summarize(*_args, **_kwargs):
            raise AssertionError("fast mode must not call the summarizer")

        monkeypatch.setattr("tools.session_search_tool._summarize_session", fail_summarize)

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {
                "id": 123,
                "session_id": "other_sid",
                "role": "user",
                "snippet": "we discussed >>>session_search<<< latency",
                "context": [
                    {"role": "user", "content": "session_search is slow"},
                    {"role": "assistant", "content": "the LLM summary is the bottleneck"},
                ],
                "source": "cli",
                "session_started": 1709400000,
                "model": "test-model",
            },
        ]
        mock_db.get_session.return_value = {"parent_session_id": None, "title": "Latency debug"}

        result = json.loads(session_search(query="session_search", db=mock_db, mode="fast"))

        assert result["success"] is True
        assert result["mode"] == "fast"
        assert result["count"] == 1
        entry = result["results"][0]
        assert entry["summary"] == "[Search hit — summary not generated in fast mode] Use snippet/context fields, or set mode='summary' for LLM-generated recall."
        assert "we discussed" not in entry["summary"]
        assert entry["model"] == "test-model"
        assert entry["snippet"] == "we discussed >>>session_search<<< latency"
        assert entry["context"][1]["content"] == "the LLM summary is the bottleneck"
        mock_db.get_messages_as_conversation.assert_not_called()

    def test_fast_mode_includes_match_message_id_for_guided_drilldown(self):
        """Fast-mode results must surface the FTS5 message id as ``match_message_id``
        so the agent can pass it back as ``around_message_id`` for a follow-up
        ``mode='guided'`` call. This is the discover → drill composition handle."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {
                "id": 987654,
                "session_id": "other_sid",
                "role": "assistant",
                "snippet": "...the >>>final design<<< was...",
                "context": [],
                "source": "cli",
                "session_started": 1709400000,
                "model": "test-model",
            },
        ]
        mock_db.get_session.return_value = {"parent_session_id": None}

        result = json.loads(session_search(query="final design", db=mock_db, mode="fast"))

        entry = result["results"][0]
        assert entry["match_message_id"] == 987654
        # Sanity: still also surfaces session_id so the agent has both pieces
        # needed to compose a guided call
        assert entry["session_id"] == "other_sid"

    @pytest.mark.parametrize("mode", ["summarized", "summarise", "summarize", "deep"])
    def test_summary_mode_aliases_use_llm_summarization_path(self, monkeypatch, mode):
        """Common natural-language mode aliases should map to summary mode."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        async def fake_summarize(_text, _query, _meta):
            return "alias summary"

        monkeypatch.setattr("tools.session_search_tool._summarize_session", fake_summarize)
        monkeypatch.setattr("model_tools._run_async", lambda coro: asyncio.run(coro))

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [{"session_id": "sid", "source": "cli"}]
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli"}
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "full transcript"},
        ]

        result = json.loads(session_search(query="session_search", db=mock_db, mode=mode))

        assert result["success"] is True
        assert result["mode"] == "summary"
        assert result["results"][0]["summary"] == "alias summary"

    @pytest.mark.parametrize("mode", ["", "unknown", 42, True, None])
    def test_invalid_or_empty_mode_falls_back_to_summary(self, monkeypatch, mode):
        """Loose tool-call args should degrade to summary mode (the safe default)."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        async def fake_summarize(_text, _query, _meta):
            return "fallback summary"

        monkeypatch.setattr("tools.session_search_tool._summarize_session", fake_summarize)
        monkeypatch.setattr("model_tools._run_async", lambda coro: asyncio.run(coro))

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "sid", "snippet": "hit", "context": "not-a-list", "source": "cli"},
        ]
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli"}
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "transcript"},
        ]

        result = json.loads(session_search(query="session_search", db=mock_db, mode=mode))

        assert result["success"] is True
        assert result["mode"] == "summary"
        assert result["results"][0]["summary"] == "fallback summary"

    def test_fast_mode_tolerates_session_metadata_lookup_failure(self):
        """Fast mode should still return the FTS hit when parent metadata is unavailable."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "sid", "snippet": "hit", "source": "cli", "model": None},
        ]
        mock_db.get_session.side_effect = RuntimeError("metadata unavailable")

        result = json.loads(session_search(query="session_search", db=mock_db, mode="fast"))

        assert result["success"] is True
        assert result["results"][0]["source"] == "cli"
        assert result["results"][0]["model"] == "unknown"

    def test_summary_mode_preserves_llm_summarization_path(self, monkeypatch):
        """Explicit summary mode keeps the previous behavior for deeper recall."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        async def fake_summarize(text, query, meta):
            assert "full transcript" in text
            assert query == "session_search"
            assert meta["source"] == "cli"
            return "focused session summary"

        monkeypatch.setattr("tools.session_search_tool._summarize_session", fake_summarize)
        monkeypatch.setattr("model_tools._run_async", lambda coro: asyncio.run(coro))

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "other_sid", "source": "cli", "session_started": 1709400000, "model": "test-model"},
        ]
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli", "started_at": 1709400000}
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "full transcript about session_search"},
        ]

        result = json.loads(session_search(query="session_search", db=mock_db, mode="summary"))

        assert result["success"] is True
        assert result["mode"] == "summary"
        assert result["results"][0]["summary"] == "focused session summary"
        mock_db.get_messages_as_conversation.assert_called_once_with("other_sid")

    def test_positional_db_argument_remains_backwards_compatible(self):
        """Keep the historical positional order: query, role_filter, limit, db, current_session_id."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = []

        result = json.loads(session_search("session_search", None, 3, mock_db, None))

        assert result["success"] is True
        assert result["mode"] == "summary"
        mock_db.search_messages.assert_called_once()

    def test_run_agent_special_session_search_paths_forward_mode(self):
        """run_agent has two direct session_search call sites outside registry dispatch.

        Both dispatch sites now pass ``mode=function_args.get("mode")`` (no
        hardcoded "summary" fallback) so that an unset mode flows through to
        the tool's normaliser, which resolves the user-configured default via
        ``_resolve_user_default_mode()``. Hardcoding "summary" at the dispatch
        layer would silently shadow that config.
        """
        from pathlib import Path

        source = (Path(__file__).parent.parent.parent / "run_agent.py").read_text()
        # Both dispatch sites pass mode= as their next-to-last group of kwargs;
        # the new guided-mode kwargs (session_id/around_message_id/window) follow.
        assert source.count('mode=function_args.get("mode")') == 2
        # And both dispatch sites carry the guided-mode handles
        assert source.count('around_message_id=function_args.get("around_message_id")') == 2
        assert source.count('window=function_args.get("window", 5)') == 2
        assert source.count('anchors=function_args.get("anchors")') == 2
        # Guard against a regression to hardcoded "summary" — the config-default
        # plumbing only works if dispatch doesn't shadow None with "summary".
        assert 'mode=function_args.get("mode", "summary")' not in source, (
            "dispatch sites must pass mode=function_args.get(\"mode\") (no default) "
            "so the user-configured default_mode can take effect"
        )

    # -----------------------------------------------------------------
    # User-configurable default mode (tools.session_search.default_mode
    # in ~/.hermes/config.yaml). Lets a user opt into fast-as-default
    # without having to pass mode= on every call.
    # -----------------------------------------------------------------

    def _clear_default_mode_cache(self):
        """Reset the lru_cache between tests so config changes are honoured."""
        from tools.session_search_tool import _resolve_user_default_mode
        _resolve_user_default_mode.cache_clear()

    def test_unset_mode_falls_back_to_summary_when_config_missing(self, monkeypatch):
        """With no config, an unset mode resolves to 'summary'."""
        from tools.session_search_tool import _resolve_user_default_mode
        self._clear_default_mode_cache()
        # Force load_config import to fail → fallback path.
        import sys
        monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
        assert _resolve_user_default_mode() == "summary"

    def test_user_can_configure_fast_as_default(self, monkeypatch):
        """tools.session_search.default_mode: fast → unset mode resolves to 'fast'."""
        from tools.session_search_tool import _resolve_user_default_mode
        self._clear_default_mode_cache()
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"tools": {"session_search": {"default_mode": "fast"}}},
        )
        assert _resolve_user_default_mode() == "fast"

    def test_user_can_configure_summary_as_default_explicitly(self, monkeypatch):
        """Explicit summary in config behaves identically to the implicit default."""
        from tools.session_search_tool import _resolve_user_default_mode
        self._clear_default_mode_cache()
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"tools": {"session_search": {"default_mode": "summary"}}},
        )
        assert _resolve_user_default_mode() == "summary"

    def test_invalid_default_mode_warns_and_falls_back(self, monkeypatch, caplog):
        """Typo'd / unknown value logs a warning and falls back to 'summary'."""
        from tools.session_search_tool import _resolve_user_default_mode
        import logging
        self._clear_default_mode_cache()
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"tools": {"session_search": {"default_mode": "smary"}}},
        )
        with caplog.at_level(logging.WARNING):
            assert _resolve_user_default_mode() == "summary"
        # User sees feedback about the typo.
        assert any("smary" in rec.message for rec in caplog.records)

    def test_guided_as_default_mode_is_rejected(self, monkeypatch):
        """guided requires anchors and can't be a standalone default — falls back to 'summary'."""
        from tools.session_search_tool import _resolve_user_default_mode
        self._clear_default_mode_cache()
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"tools": {"session_search": {"default_mode": "guided"}}},
        )
        assert _resolve_user_default_mode() == "summary"

    def test_non_string_default_mode_falls_back(self, monkeypatch):
        """Bogus types (int, dict, etc.) in YAML fall back gracefully, no crash."""
        from tools.session_search_tool import _resolve_user_default_mode
        self._clear_default_mode_cache()
        for bad in (42, ["fast"], {"mode": "fast"}, True):
            monkeypatch.setattr(
                "hermes_cli.config.load_config",
                lambda b=bad: {"tools": {"session_search": {"default_mode": b}}},
            )
            self._clear_default_mode_cache()
            assert _resolve_user_default_mode() == "summary", f"bad value {bad!r} should fall back"

    def test_explicit_mode_argument_overrides_user_default(self, monkeypatch):
        """User config sets fast-as-default, but explicit mode='summary' still wins."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        self._clear_default_mode_cache()
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"tools": {"session_search": {"default_mode": "fast"}}},
        )
        mock_db = MagicMock()
        mock_db.search_messages.return_value = []

        result = json.loads(session_search(query="anything", db=mock_db, mode="fast"))
        assert result["mode"] == "fast"
        # ...and explicit summary still produces summary even when default is fast.
        result = json.loads(session_search(query="anything", db=mock_db, mode="summary"))
        assert result["mode"] == "summary"

    def test_unset_mode_with_config_default_fast_runs_fast_path(self, monkeypatch):
        """End-to-end: config says default=fast, caller passes mode=None → fast hits returned, no LLM."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        self._clear_default_mode_cache()
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"tools": {"session_search": {"default_mode": "fast"}}},
        )

        async def fail_summarize(*_args, **_kwargs):
            raise AssertionError("fast mode must not invoke the summariser")
        monkeypatch.setattr("tools.session_search_tool._summarize_session", fail_summarize)

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "sid", "id": 7, "content": "match", "source": "cli",
             "session_started": 1709400000, "model": "test"},
        ]
        mock_db.get_session.return_value = {"id": "sid", "parent_session_id": None,
                                              "source": "cli", "started_at": 1709400000}

        # mode=None mimics what the dispatcher passes when the LLM omits 'mode'.
        result = json.loads(session_search(query="match", db=mock_db, mode=None))
        assert result["mode"] == "fast"
        assert result["count"] == 1

    def test_current_child_session_excludes_parent_lineage(self):
        """Compression/delegation parents should be excluded for the active child session."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "parent_sid", "content": "match", "source": "cli",
             "session_started": 1709500000, "model": "test"},
        ]

        def _get_session(session_id):
            if session_id == "child_sid":
                return {"parent_session_id": "parent_sid"}
            if session_id == "parent_sid":
                return {"parent_session_id": None}
            return None

        mock_db.get_session.side_effect = _get_session

        result = json.loads(session_search(
            query="test", db=mock_db, current_session_id="child_sid",
        ))

        assert result["success"] is True
        assert result["count"] == 0
        assert result["results"] == []
        assert result["sessions_searched"] == 0

    def test_limit_none_coerced_to_default(self):
        """Model sends limit=null → should fall back to 3, not TypeError."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = []

        result = json.loads(session_search(
            query="test", db=mock_db, limit=None,
        ))
        assert result["success"] is True

    def test_limit_type_object_coerced_to_default(self):
        """Model sends limit as a type object → should fall back to 3, not TypeError."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = []

        result = json.loads(session_search(
            query="test", db=mock_db, limit=int,
        ))
        assert result["success"] is True

    def test_limit_string_coerced(self):
        """Model sends limit as string '2' → should coerce to int."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = []

        result = json.loads(session_search(
            query="test", db=mock_db, limit="2",
        ))
        assert result["success"] is True

    def test_limit_clamped_to_range(self):
        """Negative or zero limit should be clamped to 1."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = []

        result = json.loads(session_search(
            query="test", db=mock_db, limit=-5,
        ))
        assert result["success"] is True

        result = json.loads(session_search(
            query="test", db=mock_db, limit=0,
        ))
        assert result["success"] is True

    def test_current_root_session_excludes_child_lineage(self):
        """Delegation child hits should be excluded when they resolve to the current root session."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "child_sid", "content": "match", "source": "cli",
             "session_started": 1709500000, "model": "test"},
        ]

        def _get_session(session_id):
            if session_id == "root_sid":
                return {"parent_session_id": None}
            if session_id == "child_sid":
                return {"parent_session_id": "root_sid"}
            return None

        mock_db.get_session.side_effect = _get_session

        result = json.loads(session_search(
            query="test", db=mock_db, current_session_id="root_sid",
        ))

        assert result["success"] is True
        assert result["count"] == 0
        assert result["results"] == []
        assert result["sessions_searched"] == 0

    def test_source_from_resolved_parent_not_fts5_child(self):
        """source in output must reflect the resolved parent session, not the child that matched FTS5.

        Regression test for #15909: when a delegation child session (source='telegram')
        resolves to a parent (source='api_server'), the result entry must report
        'api_server', not 'telegram'.

        Note: as of the match_message_id pairing fix, the result's ``session_id``
        is now the raw FTS5 sid (the only sid that pairs with match_message_id),
        and the lineage root is exposed as ``parent_session_id``. The ``source``
        promotion still comes from the resolved parent — that part of #15909 is
        unchanged.
        """
        from unittest.mock import MagicMock, AsyncMock, patch as _patch
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        # FTS5 hit is in the child delegation session which carries source='telegram'
        mock_db.search_messages.return_value = [
            {
                "session_id": "child_sid",
                "id": 42,
                "content": "hello world",
                "source": "telegram",       # child session source — wrong value to surface
                "session_started": 1709400000,
                "model": "gpt-4o-mini",
            },
        ]

        def _get_session(session_id):
            if session_id == "child_sid":
                return {
                    "id": "child_sid",
                    "parent_session_id": "parent_sid",
                    "source": "telegram",
                    "started_at": 1709400000,
                    "model": "gpt-4o-mini",
                }
            if session_id == "parent_sid":
                return {
                    "id": "parent_sid",
                    "parent_session_id": None,
                    "source": "api_server",  # correct parent source
                    "started_at": 1709300000,
                    "model": "gpt-4o-mini",
                }
            return None

        mock_db.get_session.side_effect = _get_session
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there"},
        ]

        with _patch(
            "tools.session_search_tool.async_call_llm",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no provider"),
        ):
            # Use mode='fast' to exercise the per-result (session_id,
            # match_message_id) pair shape. Summary mode doesn't return
            # match_message_id so the regression vector is fast-only.
            result = json.loads(session_search(query="hello world", db=mock_db, mode="fast"))

        assert result["success"] is True
        assert result["count"] == 1
        entry = result["results"][0]
        # Raw FTS5 sid is preserved (this is the sid that pairs with match_message_id).
        assert entry["session_id"] == "child_sid", (
            "session_id should be the raw FTS5 row's sid so it pairs with match_message_id"
        )
        # Lineage root is exposed separately.
        assert entry["parent_session_id"] == "parent_sid", (
            "parent_session_id should expose the resolved lineage root"
        )
        # match_message_id is the row's id (which lives in child_sid, not parent_sid).
        assert entry["match_message_id"] == 42
        # #15909 invariant: source still promotes from the resolved parent.
        assert entry["source"] == "api_server", (
            f"source should be parent's 'api_server', got {entry['source']!r}"
        )

    def test_fast_pair_session_id_with_match_message_id(self):
        """fast mode must emit (session_id, match_message_id) as a self-consistent pair.

        Before this fix the result reused the lineage root as session_id but
        kept the FTS5 row's id as match_message_id. The pair was unusable for
        guided drill-down because the message lives in a child session, not
        the parent. This test pins the contract: session_id is the raw sid of
        the row that contains match_message_id; parent_session_id is exposed
        separately when there's a lineage above it.
        """
        from unittest.mock import MagicMock, AsyncMock, patch as _patch
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {
                "session_id": "child_sid",
                "id": 4242,
                "content": "...query match...",
                "source": "tui",
                "session_started": 1709400000,
                "model": "test",
            },
        ]

        def _get_session(sid):
            if sid == "child_sid":
                return {"id": "child_sid", "parent_session_id": "root_sid",
                        "source": "tui", "started_at": 1709400000}
            if sid == "root_sid":
                return {"id": "root_sid", "parent_session_id": None,
                        "source": "tui", "started_at": 1709300000}
            return None

        mock_db.get_session.side_effect = _get_session

        result = json.loads(session_search(query="query match", db=mock_db, mode="fast"))
        entry = result["results"][0]

        # The pair the agent will hand back to mode='guided' must be valid.
        assert entry["session_id"] == "child_sid"
        assert entry["match_message_id"] == 4242
        # And the user-facing lineage is still discoverable.
        assert entry["parent_session_id"] == "root_sid"

    def test_fast_no_parent_session_id_field_when_session_is_already_root(self):
        """When the matching session has no parent, parent_session_id is omitted (tidy output)."""
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        mock_db = MagicMock()
        mock_db.search_messages.return_value = [
            {"session_id": "root_only", "id": 7, "content": "hit",
             "source": "cli", "session_started": 1709400000, "model": "test"},
        ]
        mock_db.get_session.return_value = {
            "id": "root_only", "parent_session_id": None, "source": "cli",
            "started_at": 1709400000,
        }

        result = json.loads(session_search(query="hit", db=mock_db, mode="fast"))
        entry = result["results"][0]
        assert entry["session_id"] == "root_only"
        assert entry["match_message_id"] == 7
        assert "parent_session_id" not in entry, (
            "parent_session_id should be absent when session has no lineage above it"
        )


# =========================================================================
# Guided mode — anchored drill-down
# =========================================================================

class TestGuidedMode:
    """Tests for mode='guided': drill into a specific session at a specific
    message id, returning a window of messages around the anchor. The
    composition flow is: agent calls fast → reads the match_message_id /
    session_id off a hit → calls back with mode='guided' to read the actual
    conversation around that point.
    """

    def _make_db(self):
        from unittest.mock import MagicMock

        db = MagicMock()
        # Default session metadata; tests can override via side_effect
        db.get_session.return_value = {
            "id": "sid",
            "parent_session_id": None,
            "source": "cli",
            "started_at": 1709400000,
            "model": "test-model",
            "title": "Some title",
        }
        return db

    def test_returns_window_around_anchor_with_metadata(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        # 5 messages around the anchor (id=200)
        db.get_messages_around.return_value = [
            {"id": 198, "role": "user", "content": "before-2"},
            {"id": 199, "role": "assistant", "content": "before-1"},
            {"id": 200, "role": "user", "content": "anchor"},
            {"id": 201, "role": "assistant", "content": "after-1"},
            {"id": 202, "role": "tool", "content": "after-2", "tool_name": "echo"},
        ]

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="sid",
            around_message_id=200,
            window=2,
        ))

        assert result["success"] is True
        assert result["mode"] == "guided"
        assert result["session_id"] == "sid"
        assert result["around_message_id"] == 200
        assert result["window"] == 2
        assert result["session_meta"]["source"] == "cli"
        assert result["session_meta"]["model"] == "test-model"
        assert result["session_meta"]["title"] == "Some title"
        # Messages preserved in order
        assert [m["id"] for m in result["messages"]] == [198, 199, 200, 201, 202]
        # Anchor flagged exactly once on the right row
        anchor_rows = [m for m in result["messages"] if m.get("anchor")]
        assert len(anchor_rows) == 1 and anchor_rows[0]["id"] == 200
        # Boundary counts
        assert result["messages_before"] == 2
        assert result["messages_after"] == 2
        # Crucially: no FTS5, no aux LLM
        db.search_messages.assert_not_called()
        db.get_messages_around.assert_called_once_with("sid", 200, window=2)

    def test_missing_session_id_returns_tool_error(self):
        from tools.session_search_tool import session_search

        db = self._make_db()

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id=None,
            around_message_id=200,
        ))

        assert result["success"] is False
        assert "session_id" in result["error"].lower()
        db.get_messages_around.assert_not_called()

    def test_missing_around_message_id_returns_tool_error(self):
        from tools.session_search_tool import session_search

        db = self._make_db()

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="sid",
            around_message_id=None,
        ))

        assert result["success"] is False
        assert "around_message_id" in result["error"].lower()
        db.get_messages_around.assert_not_called()

    def test_window_clamps_to_one_when_zero_or_negative(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        db.get_messages_around.return_value = [
            {"id": 200, "role": "user", "content": "anchor"},
        ]

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="sid",
            around_message_id=200,
            window=0,
        ))

        assert result["success"] is True
        assert result["window"] == 1
        # Confirm the clamp propagated to the DB call
        db.get_messages_around.assert_called_once_with("sid", 200, window=1)

    def test_window_clamps_to_twenty_when_too_large(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        db.get_messages_around.return_value = [
            {"id": 200, "role": "user", "content": "anchor"},
        ]

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="sid",
            around_message_id=200,
            window=999,
        ))

        assert result["success"] is True
        assert result["window"] == 20
        db.get_messages_around.assert_called_once_with("sid", 200, window=20)

    def test_session_id_not_found_returns_tool_error(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        db.get_session.return_value = None  # session doesn't exist

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="missing_sid",
            around_message_id=200,
        ))

        assert result["success"] is False
        assert "not found" in result["error"].lower()
        # No drill attempted on a non-existent session
        db.get_messages_around.assert_not_called()

    def test_around_message_id_not_in_session_returns_tool_error(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        db.get_messages_around.return_value = []  # anchor not in session
        # Make sure the rebind safety net finds no owning session either.
        db.get_session_id_for_message = lambda mid: None

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="sid",
            around_message_id=999999,
        ))

        assert result["success"] is False
        assert "around_message_id" in result["error"].lower()

    def test_guided_rebinds_anchor_when_message_lives_in_descendant_session(self):
        """Safety net: if (parent_sid, child_message_id) is passed (the broken
        shape that fast emitted before the pairing fix, and that may still
        appear from memory / legacy callers), guided locates the descendant
        session that actually owns the message and rebinds transparently.
        Rebind only happens within the same lineage; cross-lineage stays an
        error.
        """
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        db = MagicMock()

        # Lineage: parent_sid is root; child_sid is a delegation/compression child.
        def _get_session(sid):
            if sid == "parent_sid":
                return {"id": "parent_sid", "parent_session_id": None,
                        "source": "tui", "started_at": 1709400000, "title": "parent"}
            if sid == "child_sid":
                return {"id": "child_sid", "parent_session_id": "parent_sid",
                        "source": "tui", "started_at": 1709500000, "title": "child"}
            return None
        db.get_session.side_effect = _get_session

        # Message 4242 lives in child_sid, not parent_sid.
        def _get_messages_around(sid, mid, window):
            if sid == "child_sid" and mid == 4242:
                return [
                    {"id": 4240, "role": "user", "content": "before-2"},
                    {"id": 4241, "role": "assistant", "content": "before-1"},
                    {"id": 4242, "role": "tool", "content": "ANCHOR"},
                    {"id": 4243, "role": "assistant", "content": "after-1"},
                    {"id": 4244, "role": "user", "content": "after-2"},
                ]
            return []
        db.get_messages_around.side_effect = _get_messages_around

        # Safety-net lookup: which session owns message 4242? child_sid.
        db.get_session_id_for_message = lambda mid: "child_sid" if mid == 4242 else None

        # Agent passes the BROKEN pair (parent sid, child message id) — should rebind.
        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="parent_sid",
            around_message_id=4242,
        ))

        assert result["success"] is True, result
        windows = result["windows"]
        assert len(windows) == 1
        w = windows[0]
        assert w["success"] is True
        assert w["session_id"] == "child_sid", "should rebind to the owning session"
        assert w["around_message_id"] == 4242
        assert "warning" in w, "rebind must be surfaced via a warning field"
        assert "rebound" in w["warning"].lower()
        # Anchor flag is set on the right row.
        anchor = next(m for m in w["messages"] if m.get("anchor"))
        assert anchor["id"] == 4242

    def test_guided_does_not_rebind_across_lineages(self):
        """Cross-lineage rebind is rejected — only same-lineage descendants count.
        Protects against silently drilling into an unrelated session if some
        ID collision happens.
        """
        from unittest.mock import MagicMock
        from tools.session_search_tool import session_search

        db = MagicMock()

        def _get_session(sid):
            if sid == "lineage_A":
                return {"id": "lineage_A", "parent_session_id": None,
                        "started_at": 1709400000}
            if sid == "lineage_B":
                return {"id": "lineage_B", "parent_session_id": None,
                        "started_at": 1709500000}
            return None
        db.get_session.side_effect = _get_session

        # Anchor 4242 is empty in A but owned by B (different lineage).
        db.get_messages_around.return_value = []
        db.get_session_id_for_message = lambda mid: "lineage_B" if mid == 4242 else None

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="lineage_A",
            around_message_id=4242,
        ))

        # Single-anchor failure surfaces as top-level tool_error (legacy shape).
        assert result["success"] is False
        assert "not in session_id" in result["error"]

    def test_at_session_boundary_returns_partial_window(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        # Anchor near start of session — only 2 messages available before
        db.get_messages_around.return_value = [
            {"id": 1, "role": "user", "content": "first"},
            {"id": 2, "role": "assistant", "content": "second"},
            {"id": 3, "role": "user", "content": "anchor"},
            {"id": 4, "role": "assistant", "content": "after-1"},
            {"id": 5, "role": "user", "content": "after-2"},
        ]

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="sid",
            around_message_id=3,
            window=5,  # asked for 5 each side, only 2 exist on the before side
        ))

        assert result["success"] is True
        assert result["messages_before"] == 2
        assert result["messages_after"] == 2
        # Anchor is on the right row even with partial window
        anchor_rows = [m for m in result["messages"] if m.get("anchor")]
        assert len(anchor_rows) == 1 and anchor_rows[0]["id"] == 3

    def test_rejects_drill_into_current_session_lineage(self):
        """If the agent asks to drill into the very session it's running in,
        return tool_error — those messages are already in its active context.
        Same convention as fast/summary's _resolve_to_parent skip.
        """
        from tools.session_search_tool import session_search

        db = self._make_db()
        # current session = "child", parent = "root"
        # request drill into "root" → should be rejected (same lineage)
        def _get_session(sid):
            if sid == "child":
                return {"id": "child", "parent_session_id": "root"}
            if sid == "root":
                return {"id": "root", "parent_session_id": None,
                        "source": "cli", "started_at": 1709400000, "model": "test-model"}
            return None

        db.get_session.side_effect = _get_session

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            session_id="root",
            around_message_id=200,
            current_session_id="child",
        ))

        assert result["success"] is False
        assert "current session" in result["error"].lower()
        db.get_messages_around.assert_not_called()

    def test_aliases_drill_drilldown_anchor_around_normalize_to_guided(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        db.get_messages_around.return_value = [
            {"id": 200, "role": "user", "content": "anchor"},
        ]

        for alias in ("drill", "drilldown", "drill-down", "anchor", "around"):
            result = json.loads(session_search(
                query="",
                db=db,
                mode=alias,
                session_id="sid",
                around_message_id=200,
            ))
            assert result["mode"] == "guided", f"alias {alias!r} did not map to guided"

    def test_schema_advertises_guided_mode(self):
        from tools.session_search_tool import SESSION_SEARCH_SCHEMA

        mode_param = SESSION_SEARCH_SCHEMA["parameters"]["properties"]["mode"]
        assert "guided" in mode_param["enum"]
        # Description teaches the discover→drill flow
        desc = SESSION_SEARCH_SCHEMA["description"]
        assert "guided" in desc.lower()
        assert "match_message_id" in desc
        # Guided-mode-only parameters present
        props = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert "session_id" in props
        assert "around_message_id" in props
        assert "window" in props
        assert "anchors" in props


class TestGuidedModeMultiAnchor:
    """Tests for the multi-anchor guided shape (anchors=[...]).

    The agent calls fast with a wider limit, picks the most promising K hits,
    and drills into all of them in a single guided call. Each anchor produces
    its own window in the response's 'windows' array.
    """

    def _make_db(self):
        from unittest.mock import MagicMock

        db = MagicMock()
        return db

    def _stub_session(self, db, session_id):
        """Configure db.get_session to return valid metadata for this session_id."""
        existing = db.get_session.side_effect
        configured = getattr(self, "_configured_sessions", {})
        configured[session_id] = {
            "id": session_id,
            "parent_session_id": None,
            "source": "cli",
            "started_at": 1709400000,
            "model": "test-model",
            "title": f"Session {session_id}",
        }
        self._configured_sessions = configured

        def lookup(sid):
            return configured.get(sid)

        db.get_session.side_effect = lookup

    def test_two_anchors_both_succeed_returns_two_windows(self):
        from tools.session_search_tool import session_search

        db = self._make_db()
        self._stub_session(db, "sid_A")
        self._stub_session(db, "sid_B")

        # Distinct windows per anchor
        def get_messages_around(session_id, around_id, window):
            if session_id == "sid_A" and around_id == 100:
                return [
                    {"id": 99,  "role": "user",      "content": "A-before"},
                    {"id": 100, "role": "assistant", "content": "A-anchor"},
                    {"id": 101, "role": "tool",      "content": "A-after"},
                ]
            if session_id == "sid_B" and around_id == 200:
                return [
                    {"id": 199, "role": "user",      "content": "B-before"},
                    {"id": 200, "role": "assistant", "content": "B-anchor"},
                    {"id": 201, "role": "tool",      "content": "B-after"},
                ]
            return []

        db.get_messages_around.side_effect = get_messages_around

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            anchors=[
                {"session_id": "sid_A", "around_message_id": 100},
                {"session_id": "sid_B", "around_message_id": 200},
            ],
            window=1,
        ))

        assert result["success"] is True
        assert result["mode"] == "guided"
        assert result["anchor_count"] == 2
        assert len(result["windows"]) == 2

        # Both windows succeeded, each with the right anchor flagged
        win_a = next(w for w in result["windows"] if w["session_id"] == "sid_A")
        win_b = next(w for w in result["windows"] if w["session_id"] == "sid_B")
        assert win_a["success"] is True and win_b["success"] is True
        assert win_a["around_message_id"] == 100
        assert win_b["around_message_id"] == 200
        # Anchor flag on the right row in each window
        anchors_a = [m for m in win_a["messages"] if m.get("anchor")]
        anchors_b = [m for m in win_b["messages"] if m.get("anchor")]
        assert len(anchors_a) == 1 and anchors_a[0]["id"] == 100
        assert len(anchors_b) == 1 and anchors_b[0]["id"] == 200
        # Multi-anchor responses do NOT mirror a top-level session_id
        assert "session_id" not in result
        assert "messages" not in result

        # DB called once per anchor with the shared window
        assert db.get_messages_around.call_count == 2

    def test_one_anchor_fails_other_succeeds_does_not_abort(self):
        """Per-anchor failures become inline error entries; valid anchors still drill."""
        from tools.session_search_tool import session_search

        db = self._make_db()
        # Only sid_A exists; sid_BAD is not stubbed
        self._stub_session(db, "sid_A")

        def get_messages_around(session_id, around_id, window):
            if session_id == "sid_A":
                return [{"id": 100, "role": "user", "content": "anchor"}]
            return []

        db.get_messages_around.side_effect = get_messages_around

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            anchors=[
                {"session_id": "sid_A", "around_message_id": 100},
                {"session_id": "sid_BAD", "around_message_id": 999},
            ],
        ))

        # Top-level reports overall success because at least one anchor drilled
        assert result["success"] is True
        assert result["anchor_count"] == 2

        # First window succeeded, second window has an error entry
        win_a = next(w for w in result["windows"] if w.get("session_id") == "sid_A")
        win_bad = next(w for w in result["windows"] if w.get("session_id") == "sid_BAD")
        assert win_a["success"] is True
        assert win_bad["success"] is False
        assert "not found" in win_bad["error"].lower()

    def test_single_anchor_via_anchors_list_normalises_to_legacy_shape(self):
        """anchors=[{...}] with one entry should produce the same top-level
        response shape as the legacy session_id+around_message_id call."""
        from tools.session_search_tool import session_search

        db = self._make_db()
        self._stub_session(db, "sid_A")
        db.get_messages_around.return_value = [
            {"id": 100, "role": "user", "content": "anchor"},
        ]

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            anchors=[{"session_id": "sid_A", "around_message_id": 100}],
        ))

        # Single-anchor mirroring: legacy fields present at the top level
        assert result["success"] is True
        assert result["session_id"] == "sid_A"
        assert result["around_message_id"] == 100
        assert "messages" in result
        assert "session_meta" in result
        assert result["anchor_count"] == 1
        # And the windows array is also present
        assert len(result["windows"]) == 1

    def test_empty_anchors_list_returns_tool_error(self):
        from tools.session_search_tool import session_search

        db = self._make_db()

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            anchors=[],
            session_id=None,
            around_message_id=None,
        ))

        assert result["success"] is False
        assert "anchor" in result["error"].lower()

    def test_anchors_non_list_returns_tool_error(self):
        from tools.session_search_tool import session_search

        db = self._make_db()

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            anchors="not_a_list",
        ))

        assert result["success"] is False
        assert "list" in result["error"].lower()

    def test_window_clamp_shared_across_anchors(self):
        """Window is shared across all anchors and clamped once."""
        from tools.session_search_tool import session_search

        db = self._make_db()
        self._stub_session(db, "sid_A")
        self._stub_session(db, "sid_B")
        db.get_messages_around.return_value = [
            {"id": 100, "role": "user", "content": "anchor"},
        ]

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            anchors=[
                {"session_id": "sid_A", "around_message_id": 100},
                {"session_id": "sid_B", "around_message_id": 200},
            ],
            window=999,  # clamped to 20
        ))

        assert result["window"] == 20
        # Both DB calls used the clamped window
        for call_args in db.get_messages_around.call_args_list:
            assert call_args.kwargs.get("window") == 20

    def test_per_anchor_current_lineage_rejection(self):
        """One anchor can be in the current lineage and rejected while another succeeds."""
        from tools.session_search_tool import session_search

        db = self._make_db()
        # sid_root is the parent of the current session; drilling there should be rejected
        # sid_other is unrelated and should succeed
        configured = {
            "child":     {"id": "child",     "parent_session_id": "sid_root"},
            "sid_root":  {"id": "sid_root",  "parent_session_id": None,
                          "source": "cli", "started_at": 1709400000, "model": "test-model"},
            "sid_other": {"id": "sid_other", "parent_session_id": None,
                          "source": "cli", "started_at": 1709400000, "model": "test-model"},
        }
        db.get_session.side_effect = configured.get
        db.get_messages_around.return_value = [
            {"id": 100, "role": "user", "content": "anchor"},
        ]

        result = json.loads(session_search(
            query="",
            db=db,
            mode="guided",
            anchors=[
                {"session_id": "sid_root",  "around_message_id": 100},
                {"session_id": "sid_other", "around_message_id": 100},
            ],
            current_session_id="child",
        ))

        assert result["success"] is True  # at least one anchor drilled
        rejected = next(w for w in result["windows"] if w.get("session_id") == "sid_root")
        accepted = next(w for w in result["windows"] if w.get("session_id") == "sid_other")
        assert rejected["success"] is False
        assert "current session" in rejected["error"].lower()
        assert accepted["success"] is True
