"""
Interaction event logging.

This module owns the `interaction_events` table: the rich (query -> result ->
action) log that powers personalized reranking (Phase 5) and the proactive
"Now" feed (Phase 3). It is intentionally separate from the existing `events`
table — that one feeds the cognitive/activation subsystem with item-granular
signals and is write-hot from the indexer, whereas `interaction_events` is
purely client-driven and skewed toward reads at retrieval time.

Why batched inserts? The desktop client emits events for every click, hover
dwell, copy, and dismissal. A per-event POST would saturate the server with
dozens of round trips during normal browsing and inflate the event-loop load
that Fix #9 (auth off event loop) just bought back. Batching on the client,
then one transaction on the server, keeps the write rate at ~1 request / 5 s
steady-state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator

try:
    from . import database  # package import
except ImportError:  # pragma: no cover — script context
    import database  # type: ignore[no-redef]


logger = logging.getLogger(__name__)


# ── Event taxonomy ───────────────────────────────────────────────────────────
# Kept deliberately small. Adding a new type should be a conscious decision
# because every consumer (reranker, Now feed, activation) has to decide how to
# weight it. If a caller is unsure, prefer `dwell` or `copy` to a new kind.
InteractionEventType = Literal[
    "search",   # user issued a query (item_id=None, query set)
    "click",    # user clicked a result (item_id + rank_position + query set)
    "open",     # user opened the item for real (ran the file/opened in Finder)
    "dwell",    # user previewed/hovered long enough that it counts (dwell_ms)
    "dismiss",  # user explicitly dismissed or downranked a result
    "copy",     # user copied content / path from the item
]


_VALID_EVENT_TYPES = {
    "search", "click", "open", "dwell", "dismiss", "copy",
}

# Max events per batch. Sized so a transactional insert stays well under the
# SQLite 500-variable statement cap (we use 11 params per row = 4400 params
# at the ceiling, still fine) and so a lost batch does not cost more than a
# few minutes of interaction history.
MAX_BATCH_SIZE = 200


# ── Pydantic request/response models ─────────────────────────────────────────

class InteractionEventIn(BaseModel):
    """One client-emitted interaction event."""

    event_type: str = Field(..., description="One of: search, click, open, dwell, dismiss, copy")
    item_id: str | None = None
    query: str | None = Field(None, max_length=2000)
    rank_position: int | None = Field(None, ge=1, le=10_000)
    dwell_ms: int | None = Field(None, ge=0, le=86_400_000)  # cap at one day
    session_id: str | None = Field(None, max_length=128)
    client_timestamp: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("event_type")
    @classmethod
    def _check_event_type(cls, v: str) -> str:
        normalized = (v or "").strip().lower()
        if normalized not in _VALID_EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of {sorted(_VALID_EVENT_TYPES)}; got {v!r}"
            )
        return normalized


class InteractionBatchIn(BaseModel):
    events: list[InteractionEventIn] = Field(..., min_length=0, max_length=MAX_BATCH_SIZE)
    session_id: str | None = Field(None, max_length=128)  # default for events without one


class InteractionBatchOut(BaseModel):
    accepted: int
    rejected: int
    reasons: list[str] = Field(default_factory=list)


# ── Query-hash normalization ─────────────────────────────────────────────────
# Reranker + Now feed both want "the user asks this query over and over" to
# collapse to one aggregation key, so trivial whitespace, casing, and
# punctuation differences should hash to the same value.

_QUERY_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_query_for_hash(query: str | None) -> str | None:
    """Lowercase, strip non-alphanumeric, collapse whitespace. Returns None for
    empty/whitespace-only input so the hash stays NULL-joinable."""
    if not query:
        return None
    compact = _QUERY_NORMALIZE_RE.sub(" ", query.casefold()).strip()
    if not compact:
        return None
    # Re-collapse doubled spaces from the substitution
    return " ".join(compact.split())


def hash_query(query: str | None) -> str | None:
    normalized = normalize_query_for_hash(query)
    if normalized is None:
        return None
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


# ── Insertion ────────────────────────────────────────────────────────────────

def _validate_event_semantic(event: InteractionEventIn) -> str | None:
    """Cross-field validation that Pydantic can't express cleanly. Returns a
    reason string when invalid, None when fine."""
    et = event.event_type
    if et == "search" and not event.query:
        return "search event missing query"
    if et == "click" and not event.item_id:
        return "click event missing item_id"
    if et == "click" and event.rank_position is None:
        # Not fatal — client sometimes lacks position — but log once for debug.
        pass
    if et in ("open", "dwell", "dismiss", "copy") and not event.item_id:
        return f"{et} event missing item_id"
    return None


def insert_interaction_events(
    conn: sqlite3.Connection,
    events: Iterable[InteractionEventIn],
    *,
    user_id: str = "local",
    default_session_id: str | None = None,
) -> tuple[int, list[str]]:
    """Insert a batch of events in a single transaction.

    Returns (accepted_count, list_of_rejection_reasons). Rejected events do
    not abort the whole batch — we prefer to keep the good rows and surface
    the bad ones via the reasons list so the client can log and move on.
    """
    accepted = 0
    reasons: list[str] = []
    rows: list[tuple] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for idx, event in enumerate(events):
        reason = _validate_event_semantic(event)
        if reason:
            reasons.append(f"[{idx}] {reason}")
            continue

        query_hash = hash_query(event.query)
        meta_json = (
            json.dumps(event.metadata, default=str, separators=(",", ":"))
            if event.metadata
            else "{}"
        )
        session_id = event.session_id or default_session_id
        rows.append(
            (
                user_id,
                event.event_type,
                event.item_id,
                event.query,
                query_hash,
                event.rank_position,
                event.dwell_ms,
                session_id,
                event.client_timestamp,
                meta_json,
                now_iso,
            )
        )
        accepted += 1

    if rows:
        try:
            conn.executemany(
                """
                INSERT INTO interaction_events (
                    user_id, event_type, item_id, query, query_hash,
                    rank_position, dwell_ms, session_id, client_timestamp,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.exception("Failed to persist interaction_events batch")
            return 0, [f"db_error: {exc}"]

    return accepted, reasons


# ── Read helpers (consumed by Phase 3 Now feed + Phase 5 reranker) ───────────

def get_click_stats_for_item(
    conn: sqlite3.Connection,
    user_id: str,
    item_id: str,
    since_iso: str | None = None,
) -> dict[str, int]:
    """Aggregate click/open/dwell counts for one item. Small helper the
    reranker uses to build per-user boost features."""
    params: list[Any] = [user_id, item_id]
    since_clause = ""
    if since_iso:
        since_clause = " AND created_at >= ?"
        params.append(since_iso)
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN event_type = 'click'  THEN 1 ELSE 0 END) AS clicks,
            SUM(CASE WHEN event_type = 'open'   THEN 1 ELSE 0 END) AS opens,
            SUM(CASE WHEN event_type = 'copy'   THEN 1 ELSE 0 END) AS copies,
            SUM(CASE WHEN event_type = 'dismiss' THEN 1 ELSE 0 END) AS dismisses,
            COALESCE(SUM(dwell_ms), 0) AS total_dwell_ms
        FROM interaction_events
        WHERE user_id = ? AND item_id = ?{since_clause}
        """,
        params,
    ).fetchone()
    if row is None:
        return {"clicks": 0, "opens": 0, "copies": 0, "dismisses": 0, "total_dwell_ms": 0}
    return {
        "clicks": int(row["clicks"] or 0),
        "opens": int(row["opens"] or 0),
        "copies": int(row["copies"] or 0),
        "dismisses": int(row["dismisses"] or 0),
        "total_dwell_ms": int(row["total_dwell_ms"] or 0),
    }


def get_recent_items_for_user(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    since_iso: str | None = None,
    limit: int = 50,
    event_types: tuple[str, ...] = ("click", "open", "copy", "dwell"),
) -> list[dict[str, Any]]:
    """Most-recently-interacted items for a user. Used by the Now feed
    ("things you were just looking at") and by the reranker as a recency
    prior."""
    placeholders = ",".join("?" for _ in event_types)
    params: list[Any] = [user_id, *event_types]
    since_clause = ""
    if since_iso:
        since_clause = " AND created_at >= ?"
        params.append(since_iso)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT item_id, MAX(created_at) AS last_seen_at, COUNT(*) AS touches
        FROM interaction_events
        WHERE user_id = ? AND event_type IN ({placeholders})
          AND item_id IS NOT NULL{since_clause}
        GROUP BY item_id
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_click_through_for_query(
    conn: sqlite3.Connection,
    user_id: str,
    query_hash: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """For a given query hash, which items has this user clicked, and how
    often? Feeds the Phase 5 reranker's query-specific boost layer."""
    rows = conn.execute(
        """
        SELECT item_id,
               COUNT(*) AS click_count,
               AVG(rank_position) AS avg_click_position,
               MAX(created_at) AS last_click_at
        FROM interaction_events
        WHERE user_id = ?
          AND query_hash = ?
          AND event_type IN ('click', 'open')
          AND item_id IS NOT NULL
        GROUP BY item_id
        ORDER BY click_count DESC, last_click_at DESC
        LIMIT ?
        """,
        (user_id, query_hash, limit),
    ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "InteractionEventType",
    "InteractionEventIn",
    "InteractionBatchIn",
    "InteractionBatchOut",
    "MAX_BATCH_SIZE",
    "insert_interaction_events",
    "get_click_stats_for_item",
    "get_recent_items_for_user",
    "get_click_through_for_query",
    "normalize_query_for_hash",
    "hash_query",
]
