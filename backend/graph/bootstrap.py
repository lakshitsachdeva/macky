from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable

try:
    from ..cognition.heuristics import extract_graph_entities_from_item, infer_frame
    from .facts import choose_anchor_time, extract_temporal_fact_specs
    from .store import create_store, normalize_entity_key
except ImportError:
    from cognition.heuristics import extract_graph_entities_from_item, infer_frame
    from graph.facts import choose_anchor_time, extract_temporal_fact_specs
    from graph.store import create_store, normalize_entity_key


def _iter_with_progress(rows: list[dict[str, Any]], description: str) -> Iterable[dict[str, Any]]:
    try:
        from rich.progress import track

        yield from track(rows, description=description)
        return
    except Exception:
        pass

    total = len(rows)
    for index, row in enumerate(rows, start=1):
        if index == 1 or index % 250 == 0 or index == total:
            print(f"{description}: {index}/{total}")
        yield row


def _open_memory_db_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    uri = f"{resolved.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_items(memory_db_path: Path) -> list[dict[str, Any]]:
    conn = _open_memory_db_readonly(memory_db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM items
            WHERE COALESCE(is_excluded, 0) = 0
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def bootstrap_memory_db(
    memory_db_path: str | Path,
    *,
    graph_db_path: str | Path | None = None,
    graph_backend: str | None = None,
    dry_run: bool = False,
    reset: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    memory_path = Path(memory_db_path).expanduser().resolve()
    items = _load_items(memory_path)
    items.sort(key=lambda item: choose_anchor_time(item, now))

    def emit_progress(**payload: Any) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    emit_progress(
        state="running",
        phase="loading",
        processed=0,
        total=len(items),
        percent=0.0,
        message=f"loaded {len(items)} memories from memory.db",
        dry_run=bool(dry_run),
        reset=bool(reset),
    )

    total_items = len(items)
    entity_keys: set[str] = set()
    mention_keys: set[tuple[str, str]] = set()
    fact_keys: set[tuple[str, str, str, str]] = set()

    if dry_run:
        for index, item in enumerate(_iter_with_progress(items, "Bootstrapping graph layer"), start=1):
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            anchor_time = choose_anchor_time(item, now)
            frame_type, slots, _ = infer_frame(item)
            for entity in extract_graph_entities_from_item(item):
                entity_key = normalize_entity_key(entity["name"])
                if not entity_key:
                    continue
                entity_keys.add(entity_key)
                mention_keys.add((item_id, entity_key))
            for fact in extract_temporal_fact_specs(
                item,
                frame_type=frame_type,
                slots=slots,
                anchor_time=anchor_time,
            ):
                subject_key = normalize_entity_key(fact["subject"])
                object_key = normalize_entity_key(fact["object"])
                if not subject_key or not object_key:
                    continue
                entity_keys.add(subject_key)
                entity_keys.add(object_key)
                fact_keys.add((subject_key, fact["predicate"], object_key, item_id))

            if index == 1 or index % 100 == 0 or index == total_items:
                emit_progress(
                    state="running",
                    phase="processing",
                    processed=index,
                    total=total_items,
                    percent=round((index / total_items) * 100, 1) if total_items else 100.0,
                    message=f"processing {index:,} / {total_items:,} memories",
                    dry_run=True,
                    reset=bool(reset),
                )
    else:
        store = create_store(graph_db_path, backend=graph_backend)
        if reset:
            store.clear_all()
        else:
            store.clear_bootstrap_state()

        def process_items(
            *,
            upsert_entity: Callable[..., str],
            link_episode_to_entity: Callable[..., None],
            add_temporal_fact: Callable[..., str],
        ) -> None:
            for index, item in enumerate(_iter_with_progress(items, "Bootstrapping graph layer"), start=1):
                item_id = str(item.get("id") or "")
                if not item_id:
                    continue

                anchor_time = choose_anchor_time(item, now)
                frame_type, slots, _ = infer_frame(item)
                entity_id_cache: dict[str, str] = {}

                def ensure_entity(
                    name: str | None,
                    entity_type: str,
                    *,
                    aliases: list[str] | None = None,
                    attributes: dict[str, Any] | None = None,
                ) -> str | None:
                    cleaned = " ".join(str(name or "").split()).strip()
                    if not cleaned:
                        return None
                    key = normalize_entity_key(cleaned)
                    if not key:
                        return None
                    cached = entity_id_cache.get(key)
                    if cached is not None:
                        return cached
                    entity_id = upsert_entity(
                        name=cleaned,
                        entity_type=entity_type,
                        aliases=aliases,
                        attributes=attributes,
                    )
                    entity_id_cache[key] = entity_id
                    entity_keys.add(key)
                    return entity_id

                for entity in extract_graph_entities_from_item(item):
                    entity_id = ensure_entity(
                        entity["name"],
                        entity["entity_type"],
                        aliases=list(entity.get("aliases") or []),
                        attributes=dict(entity.get("attributes") or {}),
                    )
                    if entity_id is None:
                        continue
                    mention_key = (item_id, normalize_entity_key(entity["name"]))
                    if mention_key in mention_keys:
                        continue
                    mention_keys.add(mention_key)
                    link_episode_to_entity(
                        episode_id=item_id,
                        entity_id=entity_id,
                        mentioned_at=anchor_time,
                        confidence=float(entity.get("confidence", 0.75) or 0.75),
                    )

                for fact in extract_temporal_fact_specs(
                    item,
                    frame_type=frame_type,
                    slots=slots,
                    anchor_time=anchor_time,
                ):
                    subject_id = ensure_entity(fact["subject"], fact["subject_type"])
                    object_id = ensure_entity(fact["object"], fact["object_type"])
                    if subject_id is None or object_id is None or subject_id == object_id:
                        continue
                    fact_key = (
                        normalize_entity_key(fact["subject"]),
                        fact["predicate"],
                        normalize_entity_key(fact["object"]),
                        item_id,
                    )
                    if fact_key in fact_keys:
                        continue
                    fact_keys.add(fact_key)
                    add_temporal_fact(
                        subject_id=subject_id,
                        predicate=str(fact["predicate"]),
                        object_id=object_id,
                        valid_from=fact["valid_from"],
                        episode_id=item_id,
                        confidence=float(fact.get("confidence", 0.75) or 0.75),
                    )

                if index == 1 or index % 100 == 0 or index == total_items:
                    emit_progress(
                        state="running",
                        phase="processing",
                        processed=index,
                        total=total_items,
                        percent=round((index / total_items) * 100, 1) if total_items else 100.0,
                        message=f"processing {index:,} / {total_items:,} memories",
                        dry_run=False,
                        reset=bool(reset),
                    )

        if getattr(store, "backend_name", "sqlite") == "sqlite":
            with store.get_connection() as conn:
                process_items(
                    upsert_entity=lambda **kwargs: store._upsert_entity_conn(conn, **kwargs),
                    link_episode_to_entity=lambda **kwargs: store._link_episode_to_entity_conn(conn, **kwargs),
                    add_temporal_fact=lambda **kwargs: store._add_temporal_fact_conn(conn, **kwargs),
                )
        else:
            process_items(
                upsert_entity=store.upsert_entity,
                link_episode_to_entity=store.link_episode_to_entity,
                add_temporal_fact=store.add_temporal_fact,
            )

    summary = {
        "items": len(items),
        "entities": len(entity_keys),
        "episode_mentions": len(mention_keys),
        "temporal_facts": len(fact_keys),
    }
    emit_progress(
        state="completed",
        phase="complete",
        processed=summary["items"],
        total=summary["items"],
        percent=100.0,
        message="graph bootstrap complete" if not dry_run else "graph dry run complete",
        dry_run=bool(dry_run),
        reset=bool(reset),
        summary=summary,
    )
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap Recurdo's temporal graph layer")
    parser.add_argument(
        "--memory-db",
        default=str(Path.home() / ".recurdo" / "memory.db"),
        help="Path to the source memory.db file",
    )
    parser.add_argument(
        "--graph-db",
        default=None,
        help="Optional output path for graph.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the bootstrap summary without writing to graph.db",
    )
    parser.add_argument(
        "--backend",
        choices=["sqlite", "falkor", "falkordb", "falkordblite"],
        default=None,
        help="Graph backend to write. Defaults to GRAPH_STORE_BACKEND.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear graph.db completely before bootstrapping",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    summary = bootstrap_memory_db(
        args.memory_db,
        graph_db_path=args.graph_db,
        graph_backend=args.backend,
        dry_run=args.dry_run,
        reset=args.reset,
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
