#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import config
from backend.cognition.bootstrap import (
    BOOTSTRAP_COOCCURRENCE_EDGE,
    BOOTSTRAP_ENTITY_ITEM_EDGE,
)
from backend.cognition.heuristics import (
    ALLOWED_SHORT_ENTITIES,
    CUSTOM_BLOCKLIST_NORMALIZED,
    ENTITY_BLOCKLIST,
    NUMERIC_ENTITY_RE,
    PATHLIKE_ENTITY_RE,
    extract_entities_from_item,
)
from backend.cognition.store import CognitiveStore


def _normalize_name(value: str | None) -> str:
    raw = " ".join(str(value or "").split()).strip().casefold()
    cleaned = "".join(char if char.isalnum() else " " for char in raw)
    return " ".join(cleaned.split())


def _graph_db_path(path: str | None = None) -> Path:
    return Path(path or config.GRAPH_DB_PATH).expanduser()


def _memory_db_path(path: str | None = None) -> Path:
    return Path(path or config.DB_PATH).expanduser()


def _cognitive_db_path(path: str | None = None) -> Path:
    return Path(path or config.COGNITIVE_DB_PATH).expanduser()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _classify_entity(row: dict[str, Any]) -> str | None:
    name = str(row.get("name") or "")
    normalized = _normalize_name(name)
    compact = normalized.replace(" ", "")
    fact_count = int(row.get("fact_count") or 0)
    mention_edges = int(row.get("mention_edges") or 0)

    if len(name.strip()) <= 1:
        return "len<=1"
    if not normalized:
        return "empty"
    if normalized in ENTITY_BLOCKLIST or normalized in CUSTOM_BLOCKLIST_NORMALIZED:
        return "stopword/blocklist"
    if NUMERIC_ENTITY_RE.fullmatch(name.strip()):
        return "numeric"
    if PATHLIKE_ENTITY_RE.search(name):
        return "path/url/email-artifact"
    if len(compact) == 2 and compact not in ALLOWED_SHORT_ENTITIES and fact_count == 0:
        return "len==2-no-typed-facts"
    if mention_edges == 0 and fact_count == 0:
        return "orphan"
    return None


def _load_entity_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            e.id,
            e.name,
            e.entity_type,
            e.canonical_key,
            e.mention_count,
            (
                SELECT COUNT(*)
                FROM episode_mentions em
                WHERE em.entity_id = e.id
            ) AS mention_edges,
            (
                SELECT COUNT(*)
                FROM temporal_facts tf
                WHERE tf.subject_id = e.id OR tf.object_id = e.id
            ) AS fact_count
        FROM entities e
        ORDER BY e.mention_count DESC, e.name ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def collect_cleanup_candidates(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], Counter[str]]:
    candidates: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    for row in _load_entity_rows(conn):
        reason = _classify_entity(row)
        if reason is None:
            continue
        row["delete_reason"] = reason
        candidates.append(row)
        reason_counts[reason] += 1
    return candidates, reason_counts


def _delete_entity_references(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    normalized_name: str,
) -> None:
    conn.execute("DELETE FROM episode_mentions WHERE entity_id = ?", (entity_id,))
    conn.execute(
        "DELETE FROM temporal_facts WHERE subject_id = ? OR object_id = ?",
        (entity_id, entity_id),
    )
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (entity_id,))
    conn.execute(
        """
        DELETE FROM weak_signals
        WHERE lower(trim(entity_a)) = ?
           OR lower(trim(entity_b)) = ?
        """,
        (normalized_name, normalized_name),
    )
    conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))


def run_cleanup(*, execute: bool, graph_db: str | None = None) -> dict[str, Any]:
    graph_path = _graph_db_path(graph_db)
    with _connect(graph_path) as conn:
        before_entities = int(
            conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] or 0
        )
        candidates, reason_counts = collect_cleanup_candidates(conn)
        estimated_mentions_removed = sum(
            int(row.get("mention_edges") or 0) for row in candidates
        )
        estimated_facts_removed = sum(int(row.get("fact_count") or 0) for row in candidates)

        if not execute:
            print(f"Dry run against {graph_path}")
            print(f"Found {len(candidates)} entities to delete out of {before_entities}.")
            for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
                print(f"  - {reason}: {count}")
            print(
                "Estimated removals:"
                f" mentions={estimated_mentions_removed}, typed_facts={estimated_facts_removed}"
            )
            for row in candidates[:40]:
                print(
                    "  would delete "
                    f"'{row['name']}' ({row['entity_type']}, {row['mention_edges']} mentions) "
                    f"- {row['delete_reason']}"
                )
            return {
                "execute": False,
                "graph_db": str(graph_path),
                "before_entities": before_entities,
                "after_entities": before_entities,
                "deleted_entities": 0,
                "candidates": len(candidates),
                "reason_counts": dict(reason_counts),
                "estimated_mentions_removed": estimated_mentions_removed,
                "estimated_facts_removed": estimated_facts_removed,
            }

        deleted = 0
        for row in candidates:
            normalized_name = _normalize_name(str(row.get("name") or ""))
            _delete_entity_references(
                conn,
                entity_id=str(row["id"]),
                normalized_name=normalized_name,
            )
            deleted += 1
            print(
                f"Deleted entity '{row['name']}' ({row['entity_type']}, "
                f"{row['mention_edges']} mentions) - {row['delete_reason']}"
            )

        after_entities = int(
            conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] or 0
        )
        conn.commit()

    print(
        f"Deleted {deleted} entities. "
        f"Graph reduced from {before_entities} -> {after_entities} entities. "
        f"Estimated MENTIONS edges removed: {estimated_mentions_removed}"
    )
    return {
        "execute": True,
        "graph_db": str(graph_path),
        "before_entities": before_entities,
        "after_entities": after_entities,
        "deleted_entities": deleted,
        "reason_counts": dict(reason_counts),
        "estimated_mentions_removed": estimated_mentions_removed,
        "estimated_facts_removed": estimated_facts_removed,
    }


def _count_entity_network(conn: sqlite3.Connection) -> tuple[int, int]:
    edge_count = int(conn.execute("SELECT COUNT(*) FROM entity_network").fetchone()[0] or 0)
    node_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT source_id AS node_id FROM entity_network
                UNION
                SELECT target_id AS node_id FROM entity_network
            )
            """
        ).fetchone()[0]
        or 0
    )
    return node_count, edge_count


def rebuild_network(
    *,
    memory_db: str | None = None,
    cognitive_db: str | None = None,
    entity_limit_per_item: int = 12,
) -> dict[str, int]:
    memory_path = _memory_db_path(memory_db)
    cognitive_path = _cognitive_db_path(cognitive_db)
    memory_conn = _connect(memory_path)
    try:
        rows = memory_conn.execute(
            """
            SELECT *
            FROM items
            WHERE COALESCE(is_excluded, 0) = 0
            ORDER BY id ASC
            """
        ).fetchall()
        items = [dict(row) for row in rows]
    finally:
        memory_conn.close()

    edge_scores: dict[tuple[str, str, str], float] = defaultdict(float)
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        entities = extract_entities_from_item(item, limit=entity_limit_per_item)
        item_node = f"item:{item_id}"
        for entity_id, weight in entities:
            edge_scores[(entity_id, item_node, BOOTSTRAP_ENTITY_ITEM_EDGE)] += weight
            edge_scores[(item_node, entity_id, BOOTSTRAP_ENTITY_ITEM_EDGE)] += weight

        for index, (left_id, left_weight) in enumerate(entities):
            for right_id, right_weight in entities[index + 1 :]:
                pair_weight = min(1.0, (left_weight + right_weight) / 2.0)
                edge_scores[(left_id, right_id, BOOTSTRAP_COOCCURRENCE_EDGE)] += pair_weight
                edge_scores[(right_id, left_id, BOOTSTRAP_COOCCURRENCE_EDGE)] += pair_weight

    edges = [
        (source_id, target_id, min(1.0, math.log1p(weight)), edge_type)
        for (source_id, target_id, edge_type), weight in edge_scores.items()
    ]

    store = CognitiveStore(cognitive_path)
    with store.get_connection() as conn:
        prev_nodes, prev_edges = _count_entity_network(conn)
        conn.execute("DELETE FROM entity_network")
        conn.commit()

    store.bulk_upsert_entity_edges(edges)
    with store.get_connection() as conn:
        new_nodes, new_edges = _count_entity_network(conn)

    print(
        f"Network rebuilt: {new_nodes} nodes, {new_edges} edges "
        f"(was {prev_nodes} nodes, {prev_edges} edges)"
    )
    return {
        "previous_nodes": prev_nodes,
        "previous_edges": prev_edges,
        "new_nodes": new_nodes,
        "new_edges": new_edges,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean noisy entities from graph.db")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply deletes (default is dry-run).",
    )
    parser.add_argument(
        "--graph-db",
        default=None,
        help="Override graph DB path (defaults to config.GRAPH_DB_PATH).",
    )
    args = parser.parse_args()
    run_cleanup(execute=bool(args.execute), graph_db=args.graph_db)


if __name__ == "__main__":
    main()
