#!/usr/bin/env python3
"""
Session Search Tool - Long-Term Conversation Recall

Searches past session transcripts in SQLite via FTS5. Keyword search defaults
to fast snippet/context hits without any LLM call; callers can opt into focused
LLM summaries with mode="summary" when deeper recall is worth the latency.

Flow:
  1. FTS5 search finds matching messages ranked by relevance
  2. Groups by session, takes the top N unique sessions (default 3)
  3. Fast mode returns snippets and nearby context immediately
  4. Summary mode loads each session, truncates around matches, and calls an LLM
  5. Returns per-session hits/summaries with metadata
"""

import asyncio
import concurrent.futures
import json
import logging
import re
from typing import Dict, Any, List, Optional, Union

from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
MAX_SESSION_CHARS = 100_000
MAX_SUMMARY_TOKENS = 10000


def _get_session_search_max_concurrency(default: int = 3) -> int:
    """Read auxiliary.session_search.max_concurrency with sane bounds."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except ImportError:
        return default
    aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    task_config = aux.get("session_search", {}) if isinstance(aux, dict) else {}
    if not isinstance(task_config, dict):
        return default
    raw = task_config.get("max_concurrency")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 5))


def _format_timestamp(ts: Union[int, float, str, None]) -> str:
    """Convert a Unix timestamp (float/int) or ISO string to a human-readable date.

    Returns "unknown" for None, str(ts) if conversion fails.
    """
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            from datetime import datetime
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                from datetime import datetime
                dt = datetime.fromtimestamp(float(ts))
                return dt.strftime("%B %d, %Y at %I:%M %p")
            return ts
    except (ValueError, OSError, OverflowError) as e:
        # Log specific errors for debugging while gracefully handling edge cases
        logging.debug("Failed to format timestamp %s: %s", ts, e, exc_info=True)
    except Exception as e:
        logging.debug("Unexpected error formatting timestamp %s: %s", ts, e, exc_info=True)
    return str(ts)


def _format_conversation(messages: List[Dict[str, Any]]) -> str:
    """Format session messages into a readable transcript for summarization."""
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content") or ""
        tool_name = msg.get("tool_name")

        if role == "TOOL" and tool_name:
            # Truncate long tool outputs
            if len(content) > 500:
                content = content[:250] + "\n...[truncated]...\n" + content[-250:]
            parts.append(f"[TOOL:{tool_name}]: {content}")
        elif role == "ASSISTANT":
            # Include tool call names if present
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                tc_names = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = tc.get("name") or tc.get("function", {}).get("name", "?")
                        tc_names.append(name)
                if tc_names:
                    parts.append(f"[ASSISTANT]: [Called: {', '.join(tc_names)}]")
                if content:
                    parts.append(f"[ASSISTANT]: {content}")
            else:
                parts.append(f"[ASSISTANT]: {content}")
        else:
            parts.append(f"[{role}]: {content}")

    return "\n\n".join(parts)


def _truncate_around_matches(
    full_text: str, query: str, max_chars: int = MAX_SESSION_CHARS
) -> str:
    """
    Truncate a conversation transcript to *max_chars*, choosing a window
    that maximises coverage of positions where the *query* actually appears.

    Strategy (in priority order):
    1. Try to find the full query as a phrase (case-insensitive).
    2. If no phrase hit, look for positions where all query terms appear
       within a 200-char proximity window (co-occurrence).
    3. Fall back to individual term positions.

    Once candidate positions are collected the function picks the window
    start that covers the most of them.
    """
    if len(full_text) <= max_chars:
        return full_text

    text_lower = full_text.lower()
    query_lower = query.lower().strip()
    match_positions: list[int] = []

    # --- 1. Full-phrase search ------------------------------------------------
    phrase_pat = re.compile(re.escape(query_lower))
    match_positions = [m.start() for m in phrase_pat.finditer(text_lower)]

    # --- 2. Proximity co-occurrence of all terms (within 200 chars) -----------
    if not match_positions:
        terms = query_lower.split()
        if len(terms) > 1:
            # Collect every occurrence of each term
            term_positions: dict[str, list[int]] = {}
            for t in terms:
                term_positions[t] = [
                    m.start() for m in re.finditer(re.escape(t), text_lower)
                ]
            # Slide through positions of the rarest term and check proximity
            rarest = min(terms, key=lambda t: len(term_positions.get(t, [])))
            for pos in term_positions.get(rarest, []):
                if all(
                    any(abs(p - pos) < 200 for p in term_positions.get(t, []))
                    for t in terms
                    if t != rarest
                ):
                    match_positions.append(pos)

    # --- 3. Individual term positions (last resort) ---------------------------
    if not match_positions:
        terms = query_lower.split()
        for t in terms:
            for m in re.finditer(re.escape(t), text_lower):
                match_positions.append(m.start())

    if not match_positions:
        # Nothing at all — take from the start
        truncated = full_text[:max_chars]
        suffix = "\n\n...[later conversation truncated]..." if max_chars < len(full_text) else ""
        return truncated + suffix

    # --- Pick window that covers the most match positions ---------------------
    match_positions.sort()

    best_start = 0
    best_count = 0
    for candidate in match_positions:
        ws = max(0, candidate - max_chars // 4)  # bias: 25% before, 75% after
        we = ws + max_chars
        if we > len(full_text):
            ws = max(0, len(full_text) - max_chars)
            we = len(full_text)
        count = sum(1 for p in match_positions if ws <= p < we)
        if count > best_count:
            best_count = count
            best_start = ws

    start = best_start
    end = min(len(full_text), start + max_chars)

    truncated = full_text[start:end]
    prefix = "...[earlier conversation truncated]...\n\n" if start > 0 else ""
    suffix = "\n\n...[later conversation truncated]..." if end < len(full_text) else ""
    return prefix + truncated + suffix


async def _summarize_session(
    conversation_text: str, query: str, session_meta: Dict[str, Any]
) -> Optional[str]:
    """Summarize a single session conversation focused on the search query."""
    system_prompt = (
        "You are reviewing a past conversation transcript to help recall what happened. "
        "Summarize the conversation with a focus on the search topic. Include:\n"
        "1. What the user asked about or wanted to accomplish\n"
        "2. What actions were taken and what the outcomes were\n"
        "3. Key decisions, solutions found, or conclusions reached\n"
        "4. Any specific commands, files, URLs, or technical details that were important\n"
        "5. Anything left unresolved or notable\n\n"
        "Be thorough but concise. Preserve specific details (commands, paths, error messages) "
        "that would be useful to recall. Write in past tense as a factual recap."
    )

    source = session_meta.get("source", "unknown")
    started = _format_timestamp(session_meta.get("started_at"))

    user_prompt = (
        f"Search topic: {query}\n"
        f"Session source: {source}\n"
        f"Session date: {started}\n\n"
        f"CONVERSATION TRANSCRIPT:\n{conversation_text}\n\n"
        f"Summarize this conversation with focus on: {query}"
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await async_call_llm(
                task="session_search",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=MAX_SUMMARY_TOKENS,
            )
            content = extract_content_or_reasoning(response)
            if content:
                return content
            # Reasoning-only / empty — let the retry loop handle it
            logging.warning("Session search LLM returned empty content (attempt %d/%d)", attempt + 1, max_retries)
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))
                continue
            return content
        except RuntimeError:
            logging.warning("No auxiliary model available for session summarization")
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))
            else:
                logging.warning(
                    "Session summarization failed after %d attempts: %s",
                    max_retries,
                    e,
                    exc_info=True,
                )
                return None


# Sources that are excluded from session browsing/searching by default.
# Third-party integrations (Paperclip agents, etc.) tag their sessions with
# HERMES_SESSION_SOURCE=tool so they don't clutter the user's session history.
_HIDDEN_SESSION_SOURCES = ("tool",)


def _list_recent_sessions(db, limit: int, current_session_id: str = None) -> str:
    """Return metadata for the most recent sessions (no LLM calls)."""
    try:
        sessions = db.list_sessions_rich(
            limit=limit + 5,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            order_by_last_active=True,
        )  # fetch extra to skip current

        # Resolve current session lineage to exclude it
        current_root = None
        if current_session_id:
            try:
                sid = current_session_id
                visited = set()
                current_root = current_session_id
                while sid and sid not in visited:
                    visited.add(sid)
                    current_root = sid
                    s = db.get_session(sid)
                    parent = s.get("parent_session_id") if s else None
                    sid = parent if parent else None
            except Exception:
                current_root = current_session_id

        results = []
        for s in sessions:
            sid = s.get("id", "")
            if current_root and (sid == current_root or sid == current_session_id):
                continue
            # Skip child/delegation sessions (they have parent_session_id)
            if s.get("parent_session_id"):
                continue
            results.append({
                "session_id": sid,
                "title": s.get("title") or None,
                "source": s.get("source", ""),
                "started_at": s.get("started_at", ""),
                "last_active": s.get("last_active", ""),
                "message_count": s.get("message_count", 0),
                "preview": s.get("preview", ""),
            })
            if len(results) >= limit:
                break

        return json.dumps({
            "success": True,
            "mode": "recent",
            "results": results,
            "count": len(results),
            "message": f"Showing {len(results)} most recent sessions. Use a keyword query to search specific topics.",
        }, ensure_ascii=False)
    except Exception as e:
        logging.error("Error listing recent sessions: %s", e, exc_info=True)
        return tool_error(f"Failed to list recent sessions: {e}", success=False)


def _guided_drill_down(
    db,
    session_id: str,
    around_message_id,
    window: int,
    current_session_id: str = None,
) -> str:
    """Anchored drill-down for ``mode='guided'`` of ``session_search``.

    Returns a JSON string carrying a window of messages around a specific
    message id in a specific session. No FTS5, no auxiliary LLM, no
    100k-char truncation — one DB query.

    Validates: required args, session existence, anchor-in-session,
    current-lineage exclusion (the active session's history is already
    in the agent's context so a drill-in there is wasted), and clamps
    ``window`` to ``[1, 20]``.
    """
    # 1. Required-arg validation. tool_error() preserves consistent shape with
    #    the rest of session_search; missing args are user-facing failures.
    if not session_id or not isinstance(session_id, str) or not session_id.strip():
        return tool_error(
            "guided mode requires session_id (use match_message_id+session_id "
            "from a prior fast-mode hit)",
            success=False,
        )
    session_id = session_id.strip()

    # around_message_id may arrive as int or stringified int (open-source
    # models love stringifying numerics); coerce defensively.
    try:
        around_id = int(around_message_id)
    except (TypeError, ValueError):
        return tool_error(
            "guided mode requires around_message_id as an integer "
            "(use match_message_id from a prior fast-mode hit)",
            success=False,
        )

    # 2. Window clamp. Matches the existing limit-clamp pattern (silent).
    if not isinstance(window, int):
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = 5
    window = max(1, min(window, 20))

    # 3. Skip current-lineage drill-down. The agent already has the active
    #    session's messages in its context — a drill-in there returns
    #    information it already has.
    def _resolve_to_parent(sid: str) -> str:
        visited = set()
        cur = sid
        while cur and cur not in visited:
            visited.add(cur)
            try:
                meta = db.get_session(cur)
                if not meta:
                    break
                parent = meta.get("parent_session_id")
                if parent:
                    cur = parent
                else:
                    break
            except Exception as e:
                logging.debug("Error resolving parent for %s: %s", cur, e, exc_info=True)
                break
        return cur

    if current_session_id:
        current_root = _resolve_to_parent(current_session_id)
        target_root = _resolve_to_parent(session_id)
        if current_root and current_root == target_root:
            return tool_error(
                "guided mode rejects drill-down into the current session "
                "lineage — those messages are already in your active context",
                success=False,
            )

    # 4. Session existence check (separate from the anchor-in-session check
    #    so we can return a more specific error message).
    try:
        session_meta = db.get_session(session_id) or {}
    except Exception as e:
        logging.debug("get_session failed for %s: %s", session_id, e, exc_info=True)
        session_meta = {}
    if not session_meta:
        return tool_error(f"session_id not found: {session_id}", success=False)

    # 5. Fetch the window. get_messages_around() returns [] if the anchor
    #    isn't in this session — translate to a specific error.
    try:
        messages = db.get_messages_around(session_id, around_id, window=window)
    except Exception as e:
        logging.debug("get_messages_around failed: %s", e, exc_info=True)
        return tool_error(
            f"failed to load messages around {around_id} in {session_id}: {e}",
            success=False,
        )
    if not messages:
        return tool_error(
            f"around_message_id {around_id} not in session_id {session_id}",
            success=False,
        )

    # 6. Wrap with anchor flag + boundary counts so the agent can see "this is
    #    everything available" without a follow-up call.
    out_messages = []
    messages_before = 0
    messages_after = 0
    for m in messages:
        is_anchor = m.get("id") == around_id
        if not is_anchor and m.get("id", 0) < around_id:
            messages_before += 1
        elif not is_anchor:
            messages_after += 1
        out_messages.append({
            "id": m.get("id"),
            "role": m.get("role"),
            "content": m.get("content"),
            "tool_name": m.get("tool_name"),
            "tool_calls": m.get("tool_calls") or None,
            "tool_call_id": m.get("tool_call_id"),
            "timestamp": m.get("timestamp"),
            **({"anchor": True} if is_anchor else {}),
        })
        # Strip None-valued optional fields to keep payload tight
        out_messages[-1] = {k: v for k, v in out_messages[-1].items() if v is not None or k in ("content",)}

    return json.dumps({
        "success": True,
        "mode": "guided",
        "session_id": session_id,
        "around_message_id": around_id,
        "window": window,
        "session_meta": {
            "when": _format_timestamp(session_meta.get("started_at")),
            "source": session_meta.get("source"),
            "model": session_meta.get("model"),
            "title": session_meta.get("title"),
        },
        "messages": out_messages,
        "messages_before": messages_before,
        "messages_after": messages_after,
    }, ensure_ascii=False)


def session_search(
    query: str = "",
    role_filter: str = None,
    limit: int = 3,
    db=None,
    current_session_id: str = None,
    mode: str = "summary",
    # Guided-mode-only parameters: anchored drill-down into one session at one
    # specific message id. Required when mode='guided', ignored otherwise.
    session_id: str = None,
    around_message_id: int = None,
    window: int = 5,
) -> str:
    """
    Search past sessions, or drill into a specific one.

    Modes:
      * fast    — FTS5 snippets + ±1 message context. Cheap discovery.
      * summary — fetch full session(s), truncate to 100k chars, run aux LLM
                  recap. Cross-session synthesis at ~30s tool-side cost.
      * guided  — anchored drill-down. Caller supplies session_id +
                  around_message_id (typically from a prior fast hit's
                  match_message_id field) and gets a window of messages
                  around the anchor with no LLM call and no truncation.
    """
    if db is None:
        try:
            from hermes_state import SessionDB

            db = SessionDB()
        except Exception:
            logging.debug("SessionDB unavailable for session_search", exc_info=True)
            from hermes_state import format_session_db_unavailable
            return tool_error(format_session_db_unavailable(), success=False)

    mode = (mode or "summary").strip().lower() if isinstance(mode, str) else "summary"
    if mode in ("summarized", "summarise", "summarize", "deep"):
        mode = "summary"
    if mode in ("drill", "drilldown", "drill-down", "anchor", "around"):
        mode = "guided"
    if mode not in ("fast", "summary", "guided"):
        mode = "summary"

    # Guided mode is a different shape: it doesn't search, it drills. Branch
    # before FTS5 so we don't pay for anything we don't use, and so missing-arg
    # validation happens up front.
    if mode == "guided":
        return _guided_drill_down(
            db=db,
            session_id=session_id,
            around_message_id=around_message_id,
            window=window,
            current_session_id=current_session_id,
        )

    # Defensive: models (especially open-source) may send non-int limit values
    # (None when JSON null, string "int", or even a type object).  Coerce to a
    # safe integer before any arithmetic/comparison to prevent TypeError.
    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 3
    limit = max(1, min(limit, 10))  # Clamp to [1, 10]

    # Recent sessions mode: when query is empty, return metadata for recent sessions.
    # No LLM calls — just DB queries for titles, previews, timestamps.
    if not query or not query.strip():
        return _list_recent_sessions(db, limit, current_session_id)

    query = query.strip()

    try:
        # Parse role filter
        role_list = None
        if role_filter and role_filter.strip():
            role_list = [r.strip() for r in role_filter.split(",") if r.strip()]

        # FTS5 search -- get matches ranked by relevance
        raw_results = db.search_messages(
            query=query,
            role_filter=role_list,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            limit=50,  # Get more matches to find unique sessions
            offset=0,
        )

        if not raw_results:
            return json.dumps({
                "success": True,
                "mode": mode,
                "query": query,
                "results": [],
                "count": 0,
                "message": "No matching sessions found.",
            }, ensure_ascii=False)

        # Resolve child sessions to their parent — delegation stores detailed
        # content in child sessions, but the user's conversation is the parent.
        def _resolve_to_parent(session_id: str) -> str:
            """Walk delegation chain to find the root parent session ID."""
            visited = set()
            sid = session_id
            while sid and sid not in visited:
                visited.add(sid)
                try:
                    session = db.get_session(sid)
                    if not session:
                        break
                    parent = session.get("parent_session_id")
                    if parent:
                        sid = parent
                    else:
                        break
                except Exception as e:
                    logging.debug(
                        "Error resolving parent for session %s: %s",
                        sid,
                        e,
                        exc_info=True,
                    )
                    break
            return sid

        current_lineage_root = (
            _resolve_to_parent(current_session_id) if current_session_id else None
        )

        # Group by resolved (parent) session_id, dedup, skip the current
        # session lineage. Compression and delegation create child sessions
        # that still belong to the same active conversation.
        seen_sessions = {}
        for result in raw_results:
            raw_sid = result["session_id"]
            resolved_sid = _resolve_to_parent(raw_sid)
            # Skip the current session lineage — the agent already has that
            # context, even if older turns live in parent fragments.
            if current_lineage_root and resolved_sid == current_lineage_root:
                continue
            if current_session_id and raw_sid == current_session_id:
                continue
            if resolved_sid not in seen_sessions:
                result = dict(result)
                result["session_id"] = resolved_sid
                seen_sessions[resolved_sid] = result
            if len(seen_sessions) >= limit:
                break

        if mode == "fast":
            results = []
            for session_id, match_info in seen_sessions.items():
                try:
                    session_meta = db.get_session(session_id) or {}
                except Exception:
                    session_meta = {}
                snippet = match_info.get("snippet") or ""
                context = match_info.get("context") or []
                if not isinstance(context, list):
                    context = []
                results.append({
                    "session_id": session_id,
                    "when": _format_timestamp(
                        session_meta.get("started_at") or match_info.get("session_started")
                    ),
                    "source": session_meta.get("source") or match_info.get("source", "unknown"),
                    "model": session_meta.get("model") or match_info.get("model") or "unknown",
                    "matched_role": match_info.get("role"),
                    "match_message_id": match_info.get("id"),
                    "title": session_meta.get("title") or None,
                    "snippet": snippet,
                    "context": context,
                    "summary": "[Search hit — summary not generated in fast mode] Use snippet/context fields, or set mode='summary' for LLM-generated recall.",
                })

            return json.dumps({
                "success": True,
                "mode": "fast",
                "query": query,
                "results": results,
                "count": len(results),
                "sessions_searched": len(seen_sessions),
                "message": "Fast search returned FTS snippets without LLM summarization. Use mode='summary' for focused summaries when needed.",
            }, ensure_ascii=False)

        # Prepare all sessions for parallel summarization
        tasks = []
        for session_id, match_info in seen_sessions.items():
            try:
                messages = db.get_messages_as_conversation(session_id)
                if not messages:
                    continue
                session_meta = db.get_session(session_id) or {}
                conversation_text = _format_conversation(messages)
                conversation_text = _truncate_around_matches(conversation_text, query)
                tasks.append((session_id, match_info, conversation_text, session_meta))
            except Exception as e:
                logging.warning(
                    "Failed to prepare session %s: %s",
                    session_id,
                    e,
                    exc_info=True,
                )

        # Summarize all sessions in parallel
        async def _summarize_all() -> List[Union[str, Exception]]:
            """Summarize all sessions with bounded concurrency."""
            max_concurrency = min(_get_session_search_max_concurrency(), max(1, len(tasks)))
            semaphore = asyncio.Semaphore(max_concurrency)

            async def _bounded_summary(text: str, meta: Dict[str, Any]) -> Optional[str]:
                async with semaphore:
                    return await _summarize_session(text, query, meta)

            coros = [
                _bounded_summary(text, meta)
                for _, _, text, meta in tasks
            ]
            return await asyncio.gather(*coros, return_exceptions=True)

        try:
            # Use _run_async() which properly manages event loops across
            # CLI, gateway, and worker-thread contexts.  The previous
            # pattern (asyncio.run() in a ThreadPoolExecutor) created a
            # disposable event loop that conflicted with cached
            # AsyncOpenAI/httpx clients bound to a different loop,
            # causing deadlocks in gateway mode (#2681).
            from model_tools import _run_async
            results = _run_async(_summarize_all())
        except concurrent.futures.TimeoutError:
            logging.warning(
                "Session summarization timed out after 60 seconds",
                exc_info=True,
            )
            return json.dumps({
                "success": False,
                "error": "Session summarization timed out. Try a more specific query or reduce the limit.",
            }, ensure_ascii=False)

        summaries = []
        for (session_id, match_info, conversation_text, session_meta), result in zip(tasks, results):
            if isinstance(result, Exception):
                logging.warning(
                    "Failed to summarize session %s: %s",
                    session_id, result, exc_info=True,
                )
                result = None

            # Prefer resolved parent session metadata over FTS5 match metadata.
            # match_info carries source/model from the *child* session that contained
            # the FTS5 hit; after _resolve_to_parent() the session_id points to the
            # root, so session_meta has the authoritative platform/source for the
            # session the user actually cares about (#15909).
            entry = {
                "session_id": session_id,
                "when": _format_timestamp(
                    session_meta.get("started_at") or match_info.get("session_started")
                ),
                "source": session_meta.get("source") or match_info.get("source", "unknown"),
                "model": session_meta.get("model") or match_info.get("model"),
            }

            if result:
                entry["summary"] = result
            else:
                # Fallback: raw preview so matched sessions aren't silently
                # dropped when the summarizer is unavailable (fixes #3409).
                preview = (conversation_text[:500] + "\n…[truncated]") if conversation_text else "No preview available."
                entry["summary"] = f"[Raw preview — summarization unavailable]\n{preview}"

            summaries.append(entry)

        return json.dumps({
            "success": True,
            "mode": "summary",
            "query": query,
            "results": summaries,
            "count": len(summaries),
            "sessions_searched": len(seen_sessions),
        }, ensure_ascii=False)

    except Exception as e:
        logging.error("Session search failed: %s", e, exc_info=True)
        return tool_error(f"Search failed: {str(e)}", success=False)


def check_session_search_requirements() -> bool:
    """Requires SQLite state database; summary mode also needs an auxiliary model."""
    try:
        from hermes_state import DEFAULT_DB_PATH
        return DEFAULT_DB_PATH.parent.exists()
    except ImportError:
        return False


SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": (
        "Search your long-term memory of past conversations, browse recent sessions, or drill "
        "into a specific session. This is your recall -- every past session is searchable.\n\n"
        "MODES:\n"
        "1. Recent sessions (no query): Call with no arguments to see what was worked on recently. "
        "Returns titles, previews, and timestamps. Zero LLM cost, instant. "
        "Start here when the user asks what were we working on or what did we do recently.\n"
        "2. Keyword search (with query): Search for specific topics across all past sessions. "
        "Defaults to mode='summary', returning LLM-generated recaps of the matched sessions (the recall "
        "you usually want). Set mode='fast' for cheap, instant FTS snippet hits when you only need to "
        "discover which sessions touched a topic.\n"
        "3. Drill-down (mode='guided'): When a fast-mode result looks promising but you need the "
        "actual conversation around it, call again with mode='guided', session_id from the result, "
        "and around_message_id=match_message_id from the same result. Returns a window of messages "
        "around the anchor (no LLM, no truncation, ~ms latency).\n\n"
        "RECOMMENDED FLOWS:\n"
        "- 'what did we decide about X?' → mode='summary' (synthesised recall)\n"
        "- 'find the latest session about Y' → mode='fast' (cheap discovery)\n"
        "- 'I see the fast hit but want the actual back-and-forth' → mode='guided' "
        "  with session_id+around_message_id from the fast hit\n\n"
        "USE THIS PROACTIVELY when:\n"
        "- The user says 'we did this before', 'remember when', 'last time', 'as I mentioned'\n"
        "- The user asks about a topic you worked on before but don't have in current context\n"
        "- The user references a project, person, or concept that seems familiar but isn't in memory\n"
        "- You want to check if you've solved a similar problem before\n"
        "- The user asks 'what did we do about X?' or 'how did we fix Y?'\n\n"
        "Don't hesitate to search when it is actually cross-session -- summary mode is one tool call away. "
        "Better to search and confirm than to guess or ask the user to repeat themselves.\n\n"
        "Search syntax (modes 'fast' and 'summary'): keywords joined with OR for broad recall "
        "(elevenlabs OR baseten OR funding), phrases for exact match (\"docker networking\"), "
        "boolean (python NOT java), prefix (deploy*). "
        "IMPORTANT: Use OR between keywords for best results — FTS5 defaults to AND which misses "
        "sessions that only mention some terms. If a broad OR query returns nothing, try individual "
        "keyword searches in parallel."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (modes 'fast' and 'summary'). Keywords, phrases, or boolean expressions to find in past sessions. Omit this parameter entirely to browse recent sessions instead. Ignored when mode='guided'.",
            },
            "role_filter": {
                "type": "string",
                "description": "Optional: only search messages from specific roles (comma-separated). E.g. 'user,assistant' to skip tool outputs. Ignored when mode='guided'.",
            },
            "limit": {
                "type": "integer",
                "description": "Max sessions to return (default: 3, max: 10). Bump higher (5–10) when the user wants to be in the retrieval loop and pick the right anchor for a guided drill-down. Ignored when mode='guided' (which returns one anchored window per anchor).",
                "default": 3,
            },
            "mode": {
                "type": "string",
                "enum": ["fast", "summary", "guided"],
                "description": (
                    "summary (default) loads each matched session's transcript and runs the LLM "
                    "summariser to produce a focused recap — ~30s, ~3-4 KB returned per session, "
                    "surfaces cross-session synthesis (e.g. references to work sessions that didn't "
                    "themselves match FTS5). Use this when the user wants to know WHAT HAPPENED in "
                    "past sessions about a topic. "
                    "fast returns FTS5 snippets + 1-message context without any LLM call — ~10ms, "
                    "~1 KB per session, surfaces only what FTS5 directly matched. Use this when the "
                    "user only needs to discover WHICH SESSIONS touched a topic, or when you'll "
                    "drill into specific sessions yourself afterwards (then call again with mode='guided'). "
                    "guided returns a window of messages around a specific anchor in a specific session "
                    "— no LLM call, no truncation, ~ms latency. Requires session_id and "
                    "around_message_id (typically copied from a prior fast hit's match_message_id field)."
                ),
                "default": "summary",
            },
            "session_id": {
                "type": "string",
                "description": "Required for mode='guided'. The session to drill into. Copy from a prior fast-mode result.",
            },
            "around_message_id": {
                "type": "integer",
                "description": "Required for mode='guided'. The message id to anchor the window on. Copy from a prior fast-mode result's match_message_id field.",
            },
            "window": {
                "type": "integer",
                "description": "Mode='guided' only. Number of messages to return on each side of the anchor (anchor itself is always included). Clamped to [1, 20]. Default 5.",
                "default": 5,
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="session_search",
    toolset="session_search",
    schema=SESSION_SEARCH_SCHEMA,
    handler=lambda args, **kw: session_search(
        query=args.get("query") or "",
        role_filter=args.get("role_filter"),
        limit=args.get("limit", 3),
        mode=args.get("mode", "summary"),
        session_id=args.get("session_id"),
        around_message_id=args.get("around_message_id"),
        window=args.get("window", 5),
        db=kw.get("db"),
        current_session_id=kw.get("current_session_id")),
    check_fn=check_session_search_requirements,
    emoji="🔍",
)
