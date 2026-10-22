"""
Predictive "Now" feed — what the user probably wants, even before they type.

The feed is the "right side of the app": a small, curated list (7-10 items)
that updates as the user's day moves and as they interact with the library.
It's not a replacement for search — it's the thing that catches them when
they don't quite remember the right query.

Signals we blend (all pulled from tables we already have):
  1. Interaction recency — exp-decayed time since the user last clicked /
     opened / copied / dwelled on the item.            (interaction_events)
  2. Modification freshness — exp-decayed time since the file itself changed
     on disk or in Gmail.                                      (items)
  3. Hour-of-day affinity — how many past interactions for this item landed
     within ±2h of the current wall-clock hour. A proposal draft opened every
     morning at 9am rises on weekdays at 9am and sinks at midnight.
                                                       (interaction_events)
  4. Resume boost — items whose most recent interaction was a copy or a
     short open (<10s) get a small nudge: the user clearly wanted something
     out of them and may not have finished the task.    (interaction_events)

We deliberately do NOT run the FAISS/FTS retriever for /now. Three reasons:
  - There's no query; a query-less retriever pass would just rank the whole
    library by freshness, which we already have as a signal.
  - Loading FAISS on every poll trashes the page cache on low-RAM Macs.
  - Keeping /now purely metadata-driven means it can respond in <20ms even
    on a cold process, which matters for the side-panel-always-visible UX.

Diversification: after scoring we MMR-lite by source_type, so "Now" never
becomes five Gmail threads in a row when two would be plenty.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from . import database  # package import
except ImportError:  # pragma: no cover — script context
    import database  # type: ignore[no-redef]


logger = logging.getLogger(__name__)


# ── Tuning constants ─────────────────────────────────────────────────────────
# All half-lives are in hours. Picked by eyeballing a week of my own usage:
# interactions should feel "current" for a day, files should feel "fresh" for
# a week. If we ever get real telemetry we can re-fit these.
_INTERACTION_HALF_LIFE_HOURS = 24.0
_MODIFIED_HALF_LIFE_HOURS = 24.0 * 7
_HOUR_AFFINITY_WINDOW_HOURS = 2.0
_RESUME_OPEN_MAX_DWELL_MS = 10_000  # under 10s open counts as "didn't finish"

# Pool sizes. 80 recent interactions + 80 freshly-modified gives ~150 distinct
# candidates on a typical library; scoring is O(n) so this is cheap.
_RECENT_INTERACTION_POOL = 80
_MODIFIED_POOL = 80
_INTERACTION_LOOKBACK_DAYS = 14
_MODIFIED_LOOKBACK_DAYS = 21

# Blend weights. Interaction dominates because "you literally opened this
# yesterday" is a much stronger signal than "this file changed yesterday".
_W_INTERACTION = 1.0
_W_MODIFIED = 0.45
_W_HOUR_AFFINITY = 0.6
_W_RESUME = 0.35

# Diversification: how much to penalize a candidate when we've already shown
# an item of the same source_type. 0 = no diversification, 1 = strictly
# different types wins. 0.35 empirically leaves a little repetition room.
_DIVERSIFY_PENALTY = 0.35

# Minimum final score to surface an item. Below this it's noise; returning
# fewer than `limit` items is fine — a short, honest feed beats filler.
_MIN_SCORE = 0.05


# ── Score helpers ────────────────────────────────────────────────────────────

def _exp_decay(hours_elapsed: float, half_life_hours: float) -> float:
    """Classic exp decay in [0, 1]. 1.0 at 0h, 0.5 at one half-life."""
    if hours_elapsed <= 0:
        return 1.0
    if half_life_hours <= 0:
        return 0.0
    return math.exp(-math.log(2) * hours_elapsed / half_life_hours)


def _parse_iso(value: Any) -> datetime | None:
    """Forgiving ISO8601 parse. interaction_events rows use created_at in the
    'YYYY-MM-DD HH:MM:SS' SQLite default, while items.modified_at is whatever
    the pipeline dropped in. This handles both shapes + a trailing 'Z'."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # SQLite's default CURRENT_TIMESTAMP format is "YYYY-MM-DD HH:MM:SS"
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hours_since(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    delta = now - ts
    return max(0.0, delta.total_seconds() / 3600.0)


def _hour_diff(past: int, current: int) -> float:
    """Circular hour-of-day distance in [0, 12]."""
    d = abs(past - current) % 24
    return float(min(d, 24 - d))


# ── Candidate gathering ──────────────────────────────────────────────────────

def _fetch_recent_interactions(
    conn: sqlite3.Connection,
    user_id: str,
    since_iso: str,
    limit: int,
) -> list[sqlite3.Row]:
    """Latest interaction per item plus aggregate dwell/copy/open signals.

    GROUP BY item_id collapses a hot item's 50 clicks into one candidate row
    with the right "last touch" time and enough metadata to drive the
    hour-of-day and resume signals.
    """
    return conn.execute(
        """
        SELECT
            item_id,
            MAX(created_at) AS last_interaction_at,
            COUNT(*)        AS interaction_count,
            SUM(CASE WHEN event_type = 'copy' THEN 1 ELSE 0 END) AS copy_count,
            SUM(CASE WHEN event_type = 'open' AND COALESCE(dwell_ms, 0) BETWEEN 1 AND ? THEN 1 ELSE 0 END) AS short_open_count,
            MAX(CASE WHEN event_type IN ('copy','open') THEN created_at END) AS last_resume_signal_at
        FROM interaction_events
        WHERE user_id = ?
          AND item_id IS NOT NULL
          AND event_type IN ('click', 'open', 'copy', 'dwell')
          AND created_at >= ?
        GROUP BY item_id
        ORDER BY last_interaction_at DESC
        LIMIT ?
        """,
        (_RESUME_OPEN_MAX_DWELL_MS, user_id, since_iso, limit),
    ).fetchall()


def _fetch_hour_of_day_counts(
    conn: sqlite3.Connection,
    user_id: str,
    item_ids: list[str],
    since_iso: str,
    current_hour: int,
) -> dict[str, float]:
    """For each item, what fraction of recent interactions fell within ±2h of
    now? We pull the raw created_at timestamps per item and count in Python —
    SQLite's strftime('%H', ...) would work but this keeps the SQL portable
    and the rows are tiny after the date filter."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"""
        SELECT item_id, created_at
        FROM interaction_events
        WHERE user_id = ?
          AND item_id IN ({placeholders})
          AND created_at >= ?
          AND event_type IN ('click','open','copy','dwell')
        """,
        (user_id, *item_ids, since_iso),
    ).fetchall()

    totals: dict[str, int] = {}
    hits: dict[str, int] = {}
    for r in rows:
        ts = _parse_iso(r["created_at"])
        if ts is None:
            continue
        item_id = r["item_id"]
        totals[item_id] = totals.get(item_id, 0) + 1
        if _hour_diff(ts.hour, current_hour) <= _HOUR_AFFINITY_WINDOW_HOURS:
            hits[item_id] = hits.get(item_id, 0) + 1

    return {
        item_id: (hits.get(item_id, 0) / totals[item_id]) if totals[item_id] else 0.0
        for item_id in totals
    }


def _fetch_recently_modified(
    conn: sqlite3.Connection,
    since_iso: str,
    limit: int,
) -> list[sqlite3.Row]:
    """Items that changed on disk recently. We prefer modified_at but fall
    back to first_seen_at so new imports still appear in the feed even when
    the filesystem didn't record a modification (e.g. Gmail items)."""
    return conn.execute(
        """
        SELECT *
        FROM items
        WHERE COALESCE(is_excluded, 0) = 0
          AND COALESCE(modified_at, first_seen_at) >= ?
        ORDER BY COALESCE(modified_at, first_seen_at) DESC
        LIMIT ?
        """,
        (since_iso, limit),
    ).fetchall()


# ── Main entry point ─────────────────────────────────────────────────────────

def build_now_feed(
    conn: sqlite3.Connection,
    *,
    user_id: str = "local",
    now: datetime | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the top `limit` items the user probably wants right now.

    The function is pure in (user_id, now) given a fixed DB state — no writes,
    no side effects, no external calls. That's deliberate: the endpoint that
    wraps this can safely TTL-cache the result.
    """
    now = now or datetime.now(timezone.utc)
    current_hour = now.hour

    interaction_since = (now - timedelta(days=_INTERACTION_LOOKBACK_DAYS)).isoformat()
    modified_since = (now - timedelta(days=_MODIFIED_LOOKBACK_DAYS)).isoformat()

    # Gather candidate pools ----------------------------------------------------
    interaction_rows = _fetch_recent_interactions(
        conn, user_id, interaction_since, _RECENT_INTERACTION_POOL,
    )
    modified_rows = _fetch_recently_modified(conn, modified_since, _MODIFIED_POOL)

    candidate_ids: list[str] = []
    seen: set[str] = set()
    for r in interaction_rows:
        if r["item_id"] and r["item_id"] not in seen:
            candidate_ids.append(r["item_id"])
            seen.add(r["item_id"])
    for r in modified_rows:
        if r["id"] and r["id"] not in seen:
            candidate_ids.append(r["id"])
            seen.add(r["id"])

    if not candidate_ids:
        return []

    hour_affinity = _fetch_hour_of_day_counts(
        conn, user_id, candidate_ids, interaction_since, current_hour,
    )

    # Batch-fetch full item rows for scoring + response serialization -----------
    placeholders = ",".join("?" for _ in candidate_ids)
    item_rows = conn.execute(
        f"""
        SELECT *
        FROM items
        WHERE id IN ({placeholders})
          AND COALESCE(is_excluded, 0) = 0
        """,
        candidate_ids,
    ).fetchall()
    items_by_id = {r["id"]: dict(r) for r in item_rows}

    # Index the interaction summary rows so scoring is O(1) per candidate ------
    interaction_by_id: dict[str, dict[str, Any]] = {}
    for r in interaction_rows:
        if r["item_id"]:
            interaction_by_id[r["item_id"]] = dict(r)

    scored: list[dict[str, Any]] = []
    for item_id in candidate_ids:
        item = items_by_id.get(item_id)
        if not item:
            continue  # deleted or excluded since the pool was gathered

        interaction = interaction_by_id.get(item_id) or {}

        # Signal 1: interaction recency
        last_touch = _parse_iso(interaction.get("last_interaction_at"))
        hours_since_touch = _hours_since(last_touch, now)
        interaction_score = (
            _exp_decay(hours_since_touch, _INTERACTION_HALF_LIFE_HOURS)
            if hours_since_touch is not None else 0.0
        )

        # Signal 2: modification freshness. Prefer modified_at, then
        # first_seen_at — the latter ensures brand-new imports show up.
        mod_ts = _parse_iso(item.get("modified_at")) or _parse_iso(item.get("first_seen_at"))
        hours_since_mod = _hours_since(mod_ts, now)
        modified_score = (
            _exp_decay(hours_since_mod, _MODIFIED_HALF_LIFE_HOURS)
            if hours_since_mod is not None else 0.0
        )

        # Signal 3: hour-of-day affinity (fraction in [0, 1]).
        hour_score = float(hour_affinity.get(item_id, 0.0))

        # Signal 4: resume boost — copy recency or short-open trailing flag.
        resume_ts = _parse_iso(interaction.get("last_resume_signal_at"))
        resume_hours = _hours_since(resume_ts, now)
        resume_recency = (
            _exp_decay(resume_hours, _INTERACTION_HALF_LIFE_HOURS)
            if resume_hours is not None else 0.0
        )
        has_resume_signal = (
            int(interaction.get("copy_count") or 0) > 0
            or int(interaction.get("short_open_count") or 0) > 0
        )
        resume_score = resume_recency if has_resume_signal else 0.0

        blended = (
            _W_INTERACTION * interaction_score
            + _W_MODIFIED * modified_score
            + _W_HOUR_AFFINITY * hour_score
            + _W_RESUME * resume_score
        )

        scored.append({
            "item": item,
            "score": blended,
            "components": {
                "interaction": round(interaction_score, 4),
                "modified": round(modified_score, 4),
                "hour_affinity": round(hour_score, 4),
                "resume": round(resume_score, 4),
            },
            "last_interaction_at": interaction.get("last_interaction_at"),
            "interaction_count": int(interaction.get("interaction_count") or 0),
        })

    scored.sort(key=lambda s: s["score"], reverse=True)

    # Diversify by source_type. We greedily pick the highest-scoring candidate
    # and penalize subsequent ones of the same type. This is a cheap
    # approximation of MMR that doesn't need vectors.
    picked: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}
    for cand in scored:
        if cand["score"] < _MIN_SCORE and picked:
            # Once we're below the noise floor AND we already have something,
            # stop. This avoids padding a short feed with garbage just to hit
            # the limit.
            break
        source_type = str(cand["item"].get("source_type") or "other")
        prior = type_counts.get(source_type, 0)
        adjusted = cand["score"] - _DIVERSIFY_PENALTY * prior
        cand["adjusted_score"] = adjusted
        cand["reason"] = _explain(cand["components"], cand["item"])
        picked.append(cand)
        type_counts[source_type] = prior + 1

    # Re-sort by the diversified score for the final ordering.
    picked.sort(key=lambda s: s["adjusted_score"], reverse=True)
    return [_serialize(c) for c in picked[:limit]]


def _explain(components: dict[str, float], item: dict[str, Any]) -> str:
    """One-liner shown under each card so the user knows *why* we picked it.
    Rule-based on purpose — an ML confidence score would feel creepy here."""
    if components["interaction"] >= 0.5:
        return "You were just working on this"
    if components["resume"] >= 0.3:
        return "You copied from this — pick up where you left off"
    if components["hour_affinity"] >= 0.4:
        return "You usually look at this around now"
    if components["modified"] >= 0.5:
        label = item.get("source_type") or "file"
        return f"Fresh {label} — updated recently"
    if components["interaction"] > 0:
        return "Recently touched"
    return "Recently updated"


def _serialize(candidate: dict[str, Any]) -> dict[str, Any]:
    """Compact payload for the Now panel. Intentionally does NOT include the
    full extracted_text or keywords — the panel renders a title, subtitle,
    and reason, and any deeper detail comes from a subsequent /items/:id."""
    item = candidate["item"]
    return {
        "item_id": item.get("id"),
        "title": item.get("title") or item.get("filename") or item.get("id"),
        "filename": item.get("filename"),
        "source_type": item.get("source_type"),
        "file_type": item.get("file_type"),
        "modified_at": item.get("modified_at"),
        "last_interaction_at": candidate.get("last_interaction_at"),
        "interaction_count": candidate.get("interaction_count", 0),
        "score": round(float(candidate.get("adjusted_score", candidate["score"])), 4),
        "components": candidate.get("components", {}),
        "reason": candidate.get("reason", ""),
    }


__all__ = ["build_now_feed"]
