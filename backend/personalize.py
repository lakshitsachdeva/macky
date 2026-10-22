"""
Click-learning personalized reranker.

Reads interaction_events to compute a per-item boost for a given query.
The boost is additive on top of the base retrieval score so it nudges
frequently opened results higher without overriding semantic relevance.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

# Weight each event type contributes to the boost signal
_EVENT_WEIGHTS: dict[str, float] = {
    "open": 1.0,
    "click": 0.8,
    "dwell": 0.6,
    "copy": 0.5,
    "preview": 0.3,
}

# How far back to look (days) — beyond this, signal decays to zero
_LOOKBACK_DAYS = 90

# Max boost added to any single item's score (keeps reranker from
# overwhelming semantic relevance on obscure queries)
_MAX_BOOST = 0.25


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _query_hash(query: str) -> str:
    return hashlib.md5(_normalize_query(query).encode()).hexdigest()[:16]


def build_boost_vector(
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    *,
    candidate_ids: list[str] | None = None,
) -> dict[str, float]:
    """Return {item_id: boost_score} for the given query.

    boost_score is in [0, _MAX_BOOST]. Items not in the result have score 0.
    """
    if not query.strip():
        return {}

    qhash = _query_hash(query)

    try:
        # Two signal sources:
        # 1. Query-specific: items the user opened after *this* query
        # 2. Global recency: items opened frequently in the last N days
        #    (weighted less so it doesn't drown out query intent)

        rows: list[tuple[str, str, int]] = conn.execute(
            """
            SELECT item_id, event_type, COUNT(*) as cnt
            FROM interaction_events
            WHERE user_id = ?
              AND item_id IS NOT NULL
              AND event_type IN ('open','click','dwell','copy','preview')
              AND created_at >= datetime('now', ? || ' days')
              AND (
                    query_hash = ?
                    OR query_hash IS NULL
                    OR query_hash = ''
              )
            GROUP BY item_id, event_type
            """,
            (user_id, f"-{_LOOKBACK_DAYS}", qhash),
        ).fetchall()
    except Exception:
        logger.debug("personalize: interaction_events query failed", exc_info=True)
        return {}

    # Aggregate weighted counts per item
    raw: dict[str, float] = {}
    for item_id, event_type, cnt in rows:
        if candidate_ids and item_id not in candidate_ids:
            continue
        weight = _EVENT_WEIGHTS.get(event_type, 0.2)
        raw[item_id] = raw.get(item_id, 0.0) + weight * cnt

    if not raw:
        return {}

    # Normalise to [0, _MAX_BOOST] via log-squashing
    import math
    max_raw = max(raw.values()) or 1.0
    return {
        item_id: min(_MAX_BOOST, (math.log1p(score) / math.log1p(max_raw)) * _MAX_BOOST)
        for item_id, score in raw.items()
    }


def apply_boost(
    results: list[dict[str, Any]],
    boost: dict[str, float],
    *,
    score_key: str = "score",
) -> list[dict[str, Any]]:
    """Add boost to each result's score and re-sort descending."""
    if not boost:
        return results

    boosted: list[dict[str, Any]] = []
    for item in results:
        item_id = str(item.get("id") or item.get("item_id") or "")
        extra = boost.get(item_id, 0.0)
        if extra:
            item = dict(item)
            item[score_key] = float(item.get(score_key) or 0.0) + extra
            item["_personalized"] = True
        boosted.append(item)

    boosted.sort(key=lambda x: float(x.get(score_key) or 0.0), reverse=True)
    return boosted
