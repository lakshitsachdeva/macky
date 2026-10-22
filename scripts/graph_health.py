#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


DATA_DIR = Path.home() / ".recurdo"
MEMORY_DB = DATA_DIR / "memory.db"
GRAPH_DB = DATA_DIR / "graph.db"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    value = row[0]
    return int(value or 0)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _print_section(title: str, payload: Any) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(payload, indent=2, ensure_ascii=True))


def run_health_check() -> None:
    if not MEMORY_DB.exists():
        raise SystemExit(f"Missing memory DB: {MEMORY_DB}")
    if not GRAPH_DB.exists():
        raise SystemExit(f"Missing graph DB: {GRAPH_DB}")

    with _connect(MEMORY_DB) as memory_conn, _connect(GRAPH_DB) as graph_conn:
        active_items = _scalar(
            memory_conn,
            "SELECT COUNT(*) FROM items WHERE COALESCE(is_excluded, 0) = 0",
        )
        total_items = _scalar(memory_conn, "SELECT COUNT(*) FROM items")
        entities = _scalar(graph_conn, "SELECT COUNT(*) FROM entities")
        mention_edges = _scalar(graph_conn, "SELECT COUNT(*) FROM episode_mentions")
        temporal_facts = _scalar(graph_conn, "SELECT COUNT(*) FROM temporal_facts")
        weak_signals = _scalar(graph_conn, "SELECT COUNT(*) FROM weak_signals")
        promoted_signals = _scalar(graph_conn, "SELECT COUNT(*) FROM weak_signals WHERE promoted = 1")
        episode_links = _scalar(graph_conn, "SELECT COUNT(*) FROM episode_links")
        episodes_with_mentions = _scalar(
            graph_conn,
            "SELECT COUNT(DISTINCT episode_id) FROM episode_mentions",
        )
        episodes_with_facts = _scalar(
            graph_conn,
            "SELECT COUNT(DISTINCT episode_id) FROM temporal_facts",
        )
        entities_with_facts = _scalar(
            graph_conn,
            """
            SELECT COUNT(DISTINCT entity_id)
            FROM (
                SELECT subject_id AS entity_id FROM temporal_facts
                UNION ALL
                SELECT object_id AS entity_id FROM temporal_facts
            )
            """,
        )
        isolated_entities = _scalar(
            graph_conn,
            """
            SELECT COUNT(*)
            FROM entities e
            WHERE NOT EXISTS (
                SELECT 1 FROM temporal_facts tf
                WHERE tf.subject_id = e.id OR tf.object_id = e.id
            )
            """,
        )
        duplicate_fact_groups = _scalar(
            graph_conn,
            """
            SELECT COUNT(*)
            FROM (
                SELECT subject_id, predicate, object_id, COUNT(*) AS row_count
                FROM temporal_facts
                GROUP BY subject_id, predicate, object_id
                HAVING row_count > 1
            )
            """,
        )
        conflicting_subject_predicates = _scalar(
            graph_conn,
            """
            SELECT COUNT(*)
            FROM (
                SELECT subject_id, predicate, COUNT(DISTINCT object_id) AS distinct_objects
                FROM temporal_facts
                WHERE valid_until IS NULL
                GROUP BY subject_id, predicate
                HAVING distinct_objects > 1
            )
            """,
        )

        overview = {
            "memory_db": str(MEMORY_DB),
            "graph_db": str(GRAPH_DB),
            "active_items": active_items,
            "total_items": total_items,
            "entities": entities,
            "mention_edges": mention_edges,
            "temporal_facts": temporal_facts,
            "weak_signals": weak_signals,
            "promoted_signals": promoted_signals,
            "episode_links": episode_links,
            "episodes_with_mentions": episodes_with_mentions,
            "episodes_with_facts": episodes_with_facts,
            "fact_coverage_pct_of_active_items": round(
                (episodes_with_facts / active_items) * 100, 2
            )
            if active_items
            else 0.0,
            "mention_coverage_pct_of_active_items": round(
                (episodes_with_mentions / active_items) * 100, 2
            )
            if active_items
            else 0.0,
            "avg_facts_per_entity_with_facts": round(
                temporal_facts / entities_with_facts, 2
            )
            if entities_with_facts
            else 0.0,
            "isolated_entities": isolated_entities,
            "isolated_entity_pct": round((isolated_entities / entities) * 100, 2)
            if entities
            else 0.0,
        }

        entity_types = Counter(
            {
                row["entity_type"]: row["count"]
                for row in _rows(
                    graph_conn,
                    """
                    SELECT entity_type, COUNT(*) AS count
                    FROM entities
                    GROUP BY entity_type
                    ORDER BY count DESC, entity_type ASC
                    """,
                )
            }
        )

        predicates = Counter(
            {
                row["predicate"]: row["count"]
                for row in _rows(
                    graph_conn,
                    """
                    SELECT predicate, COUNT(*) AS count
                    FROM temporal_facts
                    GROUP BY predicate
                    ORDER BY count DESC, predicate ASC
                    """,
                )
            }
        )

        top_hubs = _rows(
            graph_conn,
            """
            WITH degree AS (
                SELECT subject_id AS entity_id, COUNT(*) AS degree
                FROM temporal_facts
                GROUP BY subject_id
                UNION ALL
                SELECT object_id AS entity_id, COUNT(*) AS degree
                FROM temporal_facts
                GROUP BY object_id
            )
            SELECT
                e.name,
                e.entity_type,
                SUM(degree.degree) AS degree
            FROM degree
            JOIN entities e ON e.id = degree.entity_id
            GROUP BY e.id, e.name, e.entity_type
            ORDER BY degree DESC, e.name ASC
            LIMIT 15
            """,
        )

        noisy_entities = _rows(
            graph_conn,
            """
            SELECT
                name,
                entity_type,
                mention_count,
                canonical_key
            FROM entities
            WHERE LENGTH(TRIM(name)) <= 3
               OR canonical_key GLOB '[0-9]*'
            ORDER BY mention_count DESC, name ASC
            LIMIT 20
            """,
        )

        duplicate_name_candidates = _rows(
            graph_conn,
            """
            SELECT
                lower(trim(name)) AS normalized_name,
                COUNT(*) AS count,
                GROUP_CONCAT(name, ' | ') AS variants
            FROM entities
            GROUP BY lower(trim(name))
            HAVING COUNT(*) > 1
            ORDER BY count DESC, normalized_name ASC
            LIMIT 20
            """,
        )

        alias_overlap_candidates = _rows(
            graph_conn,
            """
            SELECT
                ea.normalized_alias,
                COUNT(DISTINCT ea.entity_id) AS entity_count,
                GROUP_CONCAT(e.name, ' | ') AS entities
            FROM entity_aliases ea
            JOIN entities e ON e.id = ea.entity_id
            GROUP BY ea.normalized_alias
            HAVING COUNT(DISTINCT ea.entity_id) > 1
            ORDER BY entity_count DESC, ea.normalized_alias ASC
            LIMIT 20
            """,
        )

        temporal_quality = {
            "facts_with_valid_from": _scalar(
                graph_conn,
                "SELECT COUNT(*) FROM temporal_facts WHERE valid_from IS NOT NULL",
            ),
            "closed_facts": _scalar(
                graph_conn,
                "SELECT COUNT(*) FROM temporal_facts WHERE valid_until IS NOT NULL",
            ),
            "duplicate_fact_groups": duplicate_fact_groups,
            "conflicting_open_subject_predicates": conflicting_subject_predicates,
        }

        weak_signal_relations = Counter(
            {
                row["relation"]: row["count"]
                for row in _rows(
                    graph_conn,
                    """
                    SELECT relation, COUNT(*) AS count
                    FROM weak_signals
                    GROUP BY relation
                    ORDER BY count DESC, relation ASC
                    """,
                )
            }
        )

        episode_link_predicates = Counter(
            {
                row["predicate"]: row["count"]
                for row in _rows(
                    graph_conn,
                    """
                    SELECT predicate, COUNT(*) AS count
                    FROM episode_links
                    GROUP BY predicate
                    ORDER BY count DESC, predicate ASC
                    """,
                )
            }
        )

        _print_section("overview", overview)
        _print_section("entity_types", dict(entity_types))
        _print_section("predicates", dict(predicates))
        _print_section("weak_signal_relations", dict(weak_signal_relations))
        _print_section("episode_link_predicates", dict(episode_link_predicates))
        _print_section("top_hubs", top_hubs)
        _print_section("temporal_quality", temporal_quality)
        _print_section("duplicate_name_candidates", duplicate_name_candidates)
        _print_section("alias_overlap_candidates", alias_overlap_candidates)
        _print_section("noisy_entities", noisy_entities)


if __name__ == "__main__":
    run_health_check()
