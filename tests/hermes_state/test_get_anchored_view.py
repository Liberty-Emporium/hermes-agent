"""Unit tests for SessionDB.get_anchored_view() — window + bookends + role filter.

Used by ``session_search`` mode='guided'. Builds on ``get_messages_around``
and adds:
  - opinionated default role filter (drops tool messages from the window,
    but never drops the anchor itself)
  - session-head and session-tail bookends (default 3 messages each) so an
    FTS5 hit anywhere in a long session still yields the goal + resolution
  - bookends are skipped when the main window already overlaps the head or tail

These properties are the reason guided is useful for state recall on long
sessions, so the suite below pins them all down.
"""
import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db: SessionDB, session_id: str, roles: list[str]) -> list[int]:
    """Append messages with the given role sequence. Returns message ids."""
    db.create_session(session_id, source="cli")
    ids = []
    for i, role in enumerate(roles):
        ids.append(db.append_message(session_id, role=role, content=f"{role}-{i}"))
    return ids


def test_window_filters_tool_messages_but_keeps_anchor_when_tool(db):
    """The anchor is preserved even when its role is tool. Other tool
    messages in the window are dropped."""
    ids = _seed(db, "s1", [
        "user", "assistant", "tool",     # 0..2
        "user", "tool",                  # 3..4  ← anchor on a tool (idx 4)
        "tool", "assistant", "user",     # 5..7
    ])
    view = db.get_anchored_view("s1", ids[4], window=3, bookend=0)
    roles = [m["role"] for m in view["window"]]
    # Anchor (tool) preserved; surrounding tool messages dropped.
    assert "tool" in roles
    anchor = next(m for m in view["window"] if m["id"] == ids[4])
    assert anchor["role"] == "tool"
    # Only the anchor tool message remains — other tools filtered.
    tool_rows = [m for m in view["window"] if m["role"] == "tool"]
    assert len(tool_rows) == 1 and tool_rows[0]["id"] == ids[4]


def test_window_keeps_user_and_assistant_by_default(db):
    ids = _seed(db, "s1", ["user", "assistant"] * 6)
    view = db.get_anchored_view("s1", ids[5], window=2, bookend=0)
    # All user/assistant → all should survive the filter.
    assert {m["role"] for m in view["window"]} == {"user", "assistant"}
    assert len(view["window"]) == 5  # 2 before + anchor + 2 after


def test_bookends_returned_when_window_in_middle(db):
    ids = _seed(db, "s1", ["user", "assistant"] * 10)  # 20 messages
    view = db.get_anchored_view("s1", ids[10], window=2, bookend=3)
    assert len(view["bookend_start"]) == 3
    assert len(view["bookend_end"]) == 3
    # Bookends are the actual session head/tail.
    assert [m["id"] for m in view["bookend_start"]] == ids[:3]
    assert [m["id"] for m in view["bookend_end"]] == ids[-3:]


def test_bookend_start_empty_when_window_covers_session_head(db):
    ids = _seed(db, "s1", ["user", "assistant"] * 5)  # 10 messages
    # Anchor on id ids[1]; window=3 → covers ids[0..4]. Head overlaps.
    view = db.get_anchored_view("s1", ids[1], window=3, bookend=3)
    assert view["bookend_start"] == []
    # Tail still has space → returns bookend_end.
    assert len(view["bookend_end"]) == 3


def test_bookend_end_empty_when_window_covers_session_tail(db):
    ids = _seed(db, "s1", ["user", "assistant"] * 5)  # 10 messages
    view = db.get_anchored_view("s1", ids[-2], window=3, bookend=3)
    assert view["bookend_end"] == []
    assert len(view["bookend_start"]) == 3


def test_bookends_skip_tool_messages(db):
    ids = _seed(db, "s1", [
        "tool", "tool", "user", "assistant",     # head: only 2 user/assistant
        "user", "assistant", "user", "assistant",
        "tool", "user", "assistant", "tool",     # tail: 2 user/assistant + tool
    ])
    # Anchor in the middle; bookends should pull only user/assistant.
    view = db.get_anchored_view("s1", ids[5], window=1, bookend=3)
    assert all(m["role"] in ("user", "assistant") for m in view["bookend_start"])
    assert all(m["role"] in ("user", "assistant") for m in view["bookend_end"])


def test_bookend_zero_returns_empty_bookends(db):
    ids = _seed(db, "s1", ["user", "assistant"] * 10)
    view = db.get_anchored_view("s1", ids[10], window=2, bookend=0)
    assert view["bookend_start"] == []
    assert view["bookend_end"] == []


def test_anchor_not_in_session_returns_empty_view(db):
    ids = _seed(db, "s1", ["user", "assistant"] * 5)
    _seed(db, "s2", ["user", "assistant"] * 5)
    view = db.get_anchored_view("s1", 999999, window=3, bookend=3)
    assert view == {"window": [], "bookend_start": [], "bookend_end": []}


def test_keep_roles_none_disables_filtering(db):
    """Pass keep_roles=None to get raw window + raw bookends including tool."""
    ids = _seed(db, "s1", ["user", "tool", "assistant", "tool", "user"] * 3)
    view = db.get_anchored_view(
        "s1", ids[7], window=2, bookend=3, keep_roles=None
    )
    # Tool messages in the window survive when filtering is disabled.
    roles_in_window = [m["role"] for m in view["window"]]
    assert "tool" in roles_in_window


def test_keep_roles_can_include_tool_when_caller_wants_it(db):
    ids = _seed(db, "s1", ["user", "tool", "assistant"] * 5)
    view = db.get_anchored_view(
        "s1", ids[7], window=2, bookend=3, keep_roles=("user", "assistant", "tool")
    )
    # All three roles allowed → tool messages should now appear in the window.
    assert any(m["role"] == "tool" for m in view["window"])


def test_negative_bookend_treated_as_zero(db):
    ids = _seed(db, "s1", ["user", "assistant"] * 10)
    view = db.get_anchored_view("s1", ids[10], window=2, bookend=-3)
    assert view["bookend_start"] == []
    assert view["bookend_end"] == []


def test_bookends_do_not_leak_across_sessions(db):
    """Bookends are session-scoped. A second session with adjacent ids must
    never appear in the first session's bookends."""
    s1_ids = _seed(db, "s1", ["user", "assistant"] * 4)
    s2_ids = _seed(db, "s2", ["user", "assistant"] * 4)
    view = db.get_anchored_view("s1", s1_ids[3], window=1, bookend=3)
    bookend_ids = (
        [m["id"] for m in view["bookend_start"]]
        + [m["id"] for m in view["bookend_end"]]
    )
    assert set(bookend_ids).isdisjoint(set(s2_ids))
