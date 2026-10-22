#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import config
from backend.cognition.heuristics import extract_graph_entities_from_item, is_person_like_entity
from backend.graph.store import GraphStore

PROJECT_ROOT_PATTERNS = (
    ("Desktop/Projects", 2),
    ("Documents", 1),
    ("Desktop", 1),
)
DESKTOP_SKIP_FOLDERS = {
    "Applications",
    "Desktop",
    "Documents",
    "Downloads",
    "Library",
    "Movies",
    "Music",
    "Pictures",
    "Public",
}


def _connect_memory_db(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(path or config.DB_PATH).expanduser()))
    conn.row_factory = sqlite3.Row
    return conn


def _choose_item_time(item: dict[str, Any]) -> str:
    for field in ("created_at", "first_seen_at", "modified_at", "last_opened_at"):
        value = str(item.get(field) or "").strip()
        if not value:
            continue
        cleaned = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _project_name_from_path(original_path: str, *, home_dir: Path) -> str | None:
    if not original_path or "://" in original_path:
        return None

    raw_path = Path(original_path).expanduser()
    if not raw_path.is_absolute():
        raw_path = (home_dir / raw_path).resolve(strict=False)

    try:
        relative = raw_path.resolve(strict=False).relative_to(home_dir.resolve(strict=False))
    except ValueError:
        return None

    relative_parts = relative.parts
    for root_pattern, project_index in PROJECT_ROOT_PATTERNS:
        root_parts = tuple(root_pattern.split("/"))
        if relative_parts[: len(root_parts)] != root_parts:
            continue
        if len(relative_parts) <= project_index:
            return None
        project_name = str(relative_parts[project_index]).strip()
        if root_pattern == "Desktop" and project_name in DESKTOP_SKIP_FOLDERS:
            return None
        return project_name or None

    return None


def _load_local_items(memory_db: str | Path | None = None) -> list[dict[str, Any]]:
    conn = _connect_memory_db(memory_db)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM items
            WHERE COALESCE(is_excluded, 0) = 0
              AND source_type NOT IN ('gmail', 'gmail_attachment')
              AND original_path IS NOT NULL
              AND TRIM(original_path) != ''
            ORDER BY COALESCE(created_at, first_seen_at, modified_at, last_opened_at) ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def collect_project_groups(
    *,
    memory_db: str | Path | None = None,
    home_dir: str | Path | None = None,
    min_items: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    base_home = Path(home_dir or Path.home()).expanduser()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _load_local_items(memory_db):
        project_name = _project_name_from_path(str(item.get("original_path") or ""), home_dir=base_home)
        if not project_name:
            continue
        grouped[project_name].append(item)
    return {
        project_name: items
        for project_name, items in grouped.items()
        if len(items) >= max(1, int(min_items))
    }


def apply_project_membership(
    *,
    memory_db: str | Path | None = None,
    graph_db: str | Path | None = None,
    home_dir: str | Path | None = None,
    min_items: int = 3,
    execute: bool = False,
) -> dict[str, Any]:
    base_home = Path(home_dir or Path.home()).expanduser()
    all_grouped = defaultdict(list)
    for item in _load_local_items(memory_db):
        project_name = _project_name_from_path(str(item.get("original_path") or ""), home_dir=base_home)
        if project_name:
            all_grouped[project_name].append(item)

    eligible_groups = {
        project_name: items
        for project_name, items in all_grouped.items()
        if len(items) >= max(1, int(min_items))
    }

    summary: dict[str, Any] = {
        "execute": bool(execute),
        "memory_db": str(Path(memory_db or config.DB_PATH).expanduser()),
        "graph_db": str(Path(graph_db or config.GRAPH_DB_PATH).expanduser()),
        "project_folders": len(all_grouped),
        "eligible_project_folders": len(eligible_groups),
        "project_entities_created": 0,
        "about_facts_created": 0,
        "contributed_to_facts_created": 0,
        "samples": [
            {"project": project_name, "items": len(items)}
            for project_name, items in sorted(
                eligible_groups.items(),
                key=lambda entry: (-len(entry[1]), entry[0].casefold()),
            )[:20]
        ],
    }
    if not execute:
        return summary

    graph = GraphStore(graph_db)
    contributed_before = 0
    contributed_after = 0

    for project_name, items in eligible_groups.items():
        project_before = graph.get_entity_by_name(project_name)
        project_id = graph.upsert_entity(
            name=project_name,
            entity_type="Project",
            attributes={"source": "project_membership", "item_count": len(items)},
        )
        if project_before is None:
            summary["project_entities_created"] += 1

        project = graph.get_entity_by_name(project_name)
        if project is None:
            continue

        with graph.get_connection() as conn:
            contributed_before += int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM temporal_facts
                    WHERE predicate = 'CONTRIBUTED_TO'
                      AND object_id = ?
                    """,
                    (project_id,),
                ).fetchone()[0]
                or 0
            )

        for item in items:
            with graph.get_connection() as conn:
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM episode_mentions
                    WHERE episode_id = ?
                      AND entity_id = ?
                      AND relation_type = 'ABOUT'
                    LIMIT 1
                    """,
                    (str(item["id"]), project_id),
                ).fetchone()
                if existing is None:
                    graph._link_episode_to_entity_conn(
                        conn,
                        episode_id=str(item["id"]),
                        entity_id=project_id,
                        mentioned_at=_choose_item_time(item),
                        relation_type="ABOUT",
                        confidence=0.9,
                    )
                    summary["about_facts_created"] += 1

            for entity in extract_graph_entities_from_item(item, limit=12):
                if str(entity.get("entity_type") or "") != "Person":
                    continue
                person_name = " ".join(str(entity.get("name") or "").split()).strip()
                if not is_person_like_entity(
                    person_name,
                    aliases=list(entity.get("aliases") or []),
                    attributes=dict(entity.get("attributes") or {}),
                ):
                    continue
                graph.upsert_entity(
                    name=person_name,
                    entity_type="Person",
                    aliases=list(entity.get("aliases") or []),
                    attributes=dict(entity.get("attributes") or {}),
                )
                graph.record_weak_signal(
                    person_name,
                    project_name,
                    "WORKS_TOGETHER_ON",
                    str(item["id"]),
                    0.65,
                )

        graph.run_promotions()
        with graph.get_connection() as conn:
            contributed_after += int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM temporal_facts
                    WHERE predicate = 'CONTRIBUTED_TO'
                      AND object_id = ?
                    """,
                    (project_id,),
                ).fetchone()[0]
                or 0
            )

    summary["contributed_to_facts_created"] = max(0, contributed_after - contributed_before)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract project membership signals into graph.db")
    parser.add_argument("--memory-db", default=None, help="Override memory.db path")
    parser.add_argument("--graph-db", default=None, help="Override graph.db path")
    parser.add_argument("--home-dir", default=None, help="Override the home directory used for project-root detection")
    parser.add_argument("--min-items", type=int, default=3, help="Minimum items required to treat a folder as a project")
    parser.add_argument("--execute", action="store_true", help="Apply changes. Dry-run is the default.")
    args = parser.parse_args()

    summary = apply_project_membership(
        memory_db=args.memory_db,
        graph_db=args.graph_db,
        home_dir=args.home_dir,
        min_items=args.min_items,
        execute=bool(args.execute),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
