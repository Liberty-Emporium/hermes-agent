"""Unit tests for SessionDB.get_messages_around() — anchored message windows.

The method is used by ``session_search`` mode='guided' for anchored drill-down.
It must:
  - Return an ordered window: up to ``window`` messages before the anchor,
    the anchor itself, then up to ``window`` after, all id-ascending.
  - Honour session boundaries (fewer messages returned at start / end).
  - Honour session isolation (same id range, different session = nothing).
  - Return an empty list when the anchor is not in the named session.
"""
import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_session(db: SessionDB, session_id: str, n_messages: int):
    """Append n_messages alternating user/assistant messages to a session.

    Returns the list of message ids created (in append order).
    """
    db.create_session(session_id, source="cli")
    ids = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        msg_id = db.append_message(session_id, role=role, content=f"msg {i}")
        ids.append(msg_id)
    return ids


def test_returns_window_around_anchor_in_middle(db):
    ids = _seed_session(db, "s1", 11)
    anchor = ids[5]  # middle of 11

    result = db.get_messages_around("s1", anchor, window=3)

    # Expect 3 before + anchor + 3 after = 7 messages
    assert len(result) == 7
    # All from the right session
    assert all(m["session_id"] == "s1" for m in result)
    # Order is id ASC and contiguous
    result_ids = [m["id"] for m in result]
    assert result_ids == ids[2:9]


def test_anchor_at_first_message_returns_only_after_slice(db):
    ids = _seed_session(db, "s1", 8)
    anchor = ids[0]  # first

    result = db.get_messages_around("s1", anchor, window=3)

    # Anchor + 3 after = 4 messages, no "before"
    assert len(result) == 4
    assert [m["id"] for m in result] == ids[0:4]


def test_anchor_at_last_message_returns_only_before_slice(db):
    ids = _seed_session(db, "s1", 8)
    anchor = ids[-1]  # last

    result = db.get_messages_around("s1", anchor, window=3)

    # 3 before + anchor = 4 messages, no "after"
    assert len(result) == 4
    assert [m["id"] for m in result] == ids[-4:]


def test_anchor_not_in_session_returns_empty_list(db):
    ids = _seed_session(db, "s1", 5)
    _seed_session(db, "s2", 5)

    # Use s1 as session but pass an id that exists, just in s2
    result = db.get_messages_around("s2", ids[2], window=3)

    assert result == []


def test_does_not_leak_across_sessions(db):
    # Two sessions with adjacent message id ranges
    s1_ids = _seed_session(db, "s1", 5)
    s2_ids = _seed_session(db, "s2", 5)

    # Anchor on s1's last message — even though s2 ids are "after", they must
    # not appear in the window
    result = db.get_messages_around("s1", s1_ids[-1], window=3)

    assert all(m["session_id"] == "s1" for m in result)
    # All result ids belong to s1, not s2
    assert set(m["id"] for m in result).issubset(set(s1_ids))
    assert set(m["id"] for m in result).isdisjoint(set(s2_ids))


def test_window_larger_than_session_returns_full_session(db):
    ids = _seed_session(db, "s1", 4)
    anchor = ids[1]

    result = db.get_messages_around("s1", anchor, window=100)

    # Whole session returned, ordered ASC
    assert [m["id"] for m in result] == ids


def test_window_zero_returns_only_anchor(db):
    ids = _seed_session(db, "s1", 5)
    anchor = ids[2]

    result = db.get_messages_around("s1", anchor, window=0)

    assert len(result) == 1
    assert result[0]["id"] == anchor


def test_negative_window_treated_as_zero(db):
    ids = _seed_session(db, "s1", 5)
    anchor = ids[2]

    result = db.get_messages_around("s1", anchor, window=-3)

    assert len(result) == 1
    assert result[0]["id"] == anchor


def test_decodes_content_like_get_messages(db):
    """Content roundtrip should match get_messages's behaviour (no surprises
    for callers who switch between the two methods)."""
    ids = _seed_session(db, "s1", 3)
    anchor = ids[1]

    around = db.get_messages_around("s1", anchor, window=1)
    full = db.get_messages("s1")

    # Same rows, same content shape
    assert [m["content"] for m in around] == [m["content"] for m in full]
