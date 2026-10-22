#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.cognition.heuristics import QUERY_ENTITY_STOPWORDS, extract_query_entities
from backend.graph.falkor_store import FalkorGraphStore
from backend.graph.store import GraphStore, normalize_entity_key


DEFAULT_QUERIES = [
    "what major finance project or startup was i working on in february",
    "the stocks related project i was working on in february",
    "what was i working on in february for recurdo",
    "professor uom mailing thing",
]


def _chunks(rows: list[dict[str, Any]], size: int = 500) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index:index + size]


def _open_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path.expanduser()))
    conn.row_factory = sqlite3.Row
    return conn


def clone_sqlite_graph_to_falkor(sqlite_graph_path: Path, falkor_graph_path: Path) -> tuple[FalkorGraphStore, dict[str, int]]:
    sqlite_store = GraphStore(sqlite_graph_path)
    falkor_store = FalkorGraphStore(falkor_graph_path, graph_name="recurdo_benchmark")
    falkor_store.clear_all()

    with sqlite_store.get_connection() as conn:
        entity_rows = [dict(row) for row in conn.execute("SELECT * FROM entities").fetchall()]
        mention_rows = [dict(row) for row in conn.execute("SELECT * FROM episode_mentions").fetchall()]
        fact_rows = [dict(row) for row in conn.execute("SELECT * FROM temporal_facts").fetchall()]

    for batch in _chunks(entity_rows):
        rows = []
        for row in batch:
            aliases = json.loads(str(row.get("aliases") or "[]"))
            rows.append(
                {
                    "id": str(row["id"]),
                    "canonical_key": str(row["canonical_key"]),
                    "name": str(row["name"]),
                    "entity_type": str(row["entity_type"]),
                    "aliases": aliases,
                    "normalized_aliases": [
                        normalized
                        for normalized in dict.fromkeys(normalize_entity_key(alias) for alias in aliases)
                        if normalized
                    ],
                    "attributes_json": str(row.get("attributes") or "{}"),
                    "mention_count": int(row.get("mention_count") or 0),
                    "created_at": str(row.get("created_at") or ""),
                    "updated_at": str(row.get("updated_at") or ""),
                }
            )
        falkor_store._query(
            """
            UNWIND $rows AS row
            CREATE (:Entity {
                id: row.id,
                canonical_key: row.canonical_key,
                name: row.name,
                entity_type: row.entity_type,
                aliases: row.aliases,
                normalized_aliases: row.normalized_aliases,
                attributes_json: row.attributes_json,
                mention_count: row.mention_count,
                created_at: row.created_at,
                updated_at: row.updated_at
            })
            """,
            {"rows": rows},
            readonly=False,
        )

    for batch in _chunks(mention_rows):
        falkor_store._query(
            """
            UNWIND $rows AS row
            MERGE (ep:Episode {id: row.episode_id})
            WITH row, ep
            MATCH (e:Entity {id: row.entity_id})
            CREATE (ep)-[:MENTIONS {
                relation_type: row.relation_type,
                mentioned_at: row.mentioned_at,
                confidence: row.confidence
            }]->(e)
            """,
            {"rows": batch},
            readonly=False,
        )

    for batch in _chunks(fact_rows):
        falkor_store._query(
            """
            UNWIND $rows AS row
            MATCH (subject:Entity {id: row.subject_id})
            MATCH (object:Entity {id: row.object_id})
            CREATE (subject)-[:FACT {
                id: row.id,
                predicate: row.predicate,
                valid_from: row.valid_from,
                valid_until: row.valid_until,
                episode_id: row.episode_id,
                confidence: row.confidence,
                created_at: row.created_at
            }]->(object)
            """,
            {"rows": batch},
            readonly=False,
        )

    return falkor_store, falkor_store.get_counts()


def _query_tokens(query: str) -> list[str]:
    return [
        token
        for token in normalize_entity_key(query).split()
        if len(token) >= 3 and token not in QUERY_ENTITY_STOPWORDS
    ]


def load_candidate_items(memory_db_path: Path, query: str, *, limit: int = 80) -> dict[str, dict[str, Any]]:
    tokens = _query_tokens(query)
    with _open_sqlite(memory_db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, filename, summary, keywords, preview_text, created_at, modified_at, first_seen_at
            FROM items
            WHERE COALESCE(is_excluded, 0) = 0
            """
        ).fetchall()

    scored: list[tuple[float, dict[str, Any]]] = []
    wants_february = "february" in normalize_entity_key(query)
    for row in rows:
        item = dict(row)
        haystack = normalize_entity_key(
            " ".join(
                str(item.get(field) or "")
                for field in ("title", "filename", "summary", "keywords", "preview_text")
            )
        )
        score = sum(1.0 for token in tokens if token in haystack)
        if wants_february:
            dates = " ".join(str(item.get(field) or "") for field in ("created_at", "modified_at", "first_seen_at"))
            if "-02-" in dates:
                score += 1.5
        if score <= 0:
            continue
        scored.append((score, item))

    scored.sort(
        key=lambda entry: (
            entry[0],
            str(entry[1].get("modified_at") or entry[1].get("first_seen_at") or ""),
        ),
        reverse=True,
    )
    return {str(item["id"]): item for _, item in scored[:limit] if item.get("id")}


def _ranked_titles(scores: dict[str, float], items: dict[str, dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    ranked = sorted(scores.items(), key=lambda entry: entry[1], reverse=True)
    return [
        {
            "score": score,
            "title": str(items[item_id].get("title") or items[item_id].get("filename") or item_id),
            "filename": str(items[item_id].get("filename") or ""),
        }
        for item_id, score in ranked[:limit]
        if item_id in items
    ]


def evaluate_query(
    query: str,
    *,
    memory_db_path: Path,
    sqlite_store: GraphStore,
    falkor_store: FalkorGraphStore,
) -> dict[str, Any]:
    items = load_candidate_items(memory_db_path, query)
    item_ids = list(items)
    query_entities = extract_query_entities(query)

    sqlite_start = time.perf_counter()
    sqlite_scores = sqlite_store.score_items_for_query_entities(query_entities, item_ids)["scores"]
    sqlite_ms = round((time.perf_counter() - sqlite_start) * 1000, 2)

    falkor_start = time.perf_counter()
    falkor_scores = falkor_store.score_items_for_query_entities(query_entities, item_ids)["scores"]
    falkor_ms = round((time.perf_counter() - falkor_start) * 1000, 2)

    sqlite_top_ids = [item["filename"] for item in _ranked_titles(sqlite_scores, items, limit=5)]
    falkor_top_ids = [item["filename"] for item in _ranked_titles(falkor_scores, items, limit=5)]
    overlap = len(set(sqlite_top_ids) & set(falkor_top_ids))

    return {
        "query": query,
        "query_entities": query_entities,
        "candidates": len(item_ids),
        "sqlite_ms": sqlite_ms,
        "falkor_ms": falkor_ms,
        "top5_overlap": overlap,
        "sqlite_top": _ranked_titles(sqlite_scores, items),
        "falkor_top": _ranked_titles(falkor_scores, items),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark SQLite graph scoring against FalkorDBLite")
    parser.add_argument("--memory-db", default=str(Path.home() / ".recurdo" / "memory.db"))
    parser.add_argument("--graph-db", default=str(Path.home() / ".recurdo" / "graph.db"))
    parser.add_argument("--falkor-db", default="")
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    memory_db_path = Path(args.memory_db).expanduser()
    sqlite_graph_path = Path(args.graph_db).expanduser()
    falkor_path = (
        Path(args.falkor_db).expanduser()
        if args.falkor_db
        else Path(tempfile.mkdtemp(prefix="recurdo-falkor-bench-")) / "graph.db"
    )

    clone_start = time.perf_counter()
    falkor_store, falkor_counts = clone_sqlite_graph_to_falkor(sqlite_graph_path, falkor_path)
    clone_ms = round((time.perf_counter() - clone_start) * 1000, 2)
    sqlite_store = GraphStore(sqlite_graph_path)
    queries = args.query or DEFAULT_QUERIES

    results = [
        evaluate_query(
            query,
            memory_db_path=memory_db_path,
            sqlite_store=sqlite_store,
            falkor_store=falkor_store,
        )
        for query in queries
    ]
    payload = {
        "memory_db": str(memory_db_path),
        "sqlite_graph_db": str(sqlite_graph_path),
        "falkor_graph_db": str(falkor_path),
        "clone_ms": clone_ms,
        "falkor_counts": falkor_counts,
        "results": results,
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0

    print(f"Cloned SQLite graph to FalkorDBLite in {clone_ms} ms")
    print(f"Falkor counts: {falkor_counts}")
    for result in results:
        print()
        print(result["query"])
        print(f"  entities: {result['query_entities']}")
        print(f"  candidates: {result['candidates']}")
        print(f"  SQLite graph scoring: {result['sqlite_ms']} ms")
        print(f"  Falkor graph scoring: {result['falkor_ms']} ms")
        print(f"  top-5 overlap: {result['top5_overlap']}/5")
        print("  SQLite top:")
        for item in result["sqlite_top"][:5]:
            print(f"    {item['score']:.4f} {item['title']}")
        print("  Falkor top:")
        for item in result["falkor_top"][:5]:
            print(f"    {item['score']:.4f} {item['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
