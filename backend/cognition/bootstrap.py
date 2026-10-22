from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .heuristics import extract_entities_from_item, infer_frame
from .store import CognitiveStore


BOOTSTRAP_CREATED_EVENT = "bootstrap:created"
BOOTSTRAP_OPEN_EVENT = "bootstrap:open"
BOOTSTRAP_ENTITY_ITEM_EDGE = "entity_item"
BOOTSTRAP_COOCCURRENCE_EDGE = "co_occurrence"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _choose_anchor_time(item: dict[str, Any], now: datetime) -> datetime:
    candidates = [
        _parse_iso(item.get("first_seen_at")),
        _parse_iso(item.get("created_at")),
        _parse_iso(item.get("modified_at")),
    ]
    available = [candidate for candidate in candidates if candidate is not None]
    return min(available) if available else now


def _distribute_access_times(start: datetime, end: datetime, count: int) -> list[datetime]:
    if count <= 0:
        return []
    if end <= start:
        return [end for _ in range(count)]
    if count == 1:
        return [end]

    total_seconds = max(1.0, (end - start).total_seconds())
    step_seconds = total_seconds / max(1, count - 1)
    return [
        start + timedelta(seconds=step_seconds * index)
        for index in range(count)
    ]


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
            ORDER BY first_seen_at DESC, created_at DESC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _bootstrap_events_for_item(
    item: dict[str, Any],
    *,
    now: datetime,
) -> list[tuple[str, str, float, str, str | None]]:
    item_id = str(item.get("id") or "")
    if not item_id:
        return []

    anchor = _choose_anchor_time(item, now)
    last_opened = _parse_iso(item.get("last_opened_at"))
    modified = _parse_iso(item.get("modified_at"))
    end_time = min(now, last_opened or modified or now)
    if end_time < anchor:
        end_time = anchor

    events: list[tuple[str, str, float, str, str | None]] = [
        (
            item_id,
            BOOTSTRAP_CREATED_EVENT,
            0.25,
            anchor.isoformat(),
            json.dumps({"source": "bootstrap", "kind": "created"}, ensure_ascii=True),
        )
    ]

    open_count = int(item.get("open_count") or 0)
    if open_count > 0:
        sample_count = min(open_count, 32)
        weight_per_event = max(0.25, open_count / sample_count)
        for occurred_at in _distribute_access_times(anchor, end_time, sample_count):
            events.append(
                (
                    item_id,
                    BOOTSTRAP_OPEN_EVENT,
                    float(weight_per_event),
                    occurred_at.isoformat(),
                    json.dumps({"source": "bootstrap", "kind": "open_count"}, ensure_ascii=True),
                )
            )
    elif last_opened is not None:
        events.append(
            (
                item_id,
                BOOTSTRAP_OPEN_EVENT,
                0.8,
                last_opened.isoformat(),
                json.dumps({"source": "bootstrap", "kind": "last_opened"}, ensure_ascii=True),
            )
        )

    dwell_seconds = int(item.get("total_dwell_seconds") or 0)
    copy_events = int(item.get("copy_event_count") or 0)
    engagement_weight = min(1.5, (dwell_seconds / 1800.0) + (copy_events * 0.25))
    if engagement_weight > 0:
        events.append(
            (
                item_id,
                BOOTSTRAP_OPEN_EVENT,
                float(engagement_weight),
                end_time.isoformat(),
                json.dumps({"source": "bootstrap", "kind": "engagement"}, ensure_ascii=True),
            )
        )

    return events


def bootstrap_memory_db(
    memory_db_path: str | Path,
    *,
    cognitive_db_path: str | Path | None = None,
    dry_run: bool = False,
    reset: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    memory_path = Path(memory_db_path).expanduser().resolve()
    items = _load_items(memory_path)

    def emit_progress(**payload: Any) -> None:
        if progress_callback is None:
            return
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

    event_rows: list[tuple[str, str, float, str, str | None]] = []
    frame_rows: list[tuple[str, str, str, float]] = []
    edge_scores: dict[tuple[str, str, str], float] = defaultdict(float)

    total_items = len(items)
    for index, item in enumerate(_iter_with_progress(items, "Bootstrapping cognitive layer"), start=1):
        item_id = str(item.get("id") or "")
        if not item_id:
            continue

        event_rows.extend(_bootstrap_events_for_item(item, now=now))

        frame_type, slots, confidence = infer_frame(item)
        frame_rows.append(
            (
                item_id,
                frame_type,
                json.dumps(slots, ensure_ascii=True, sort_keys=True),
                confidence,
            )
        )

        entities = extract_entities_from_item(item)
        item_node = f"item:{item_id}"
        for entity_id, weight in entities:
            edge_scores[(entity_id, item_node, BOOTSTRAP_ENTITY_ITEM_EDGE)] += weight
            edge_scores[(item_node, entity_id, BOOTSTRAP_ENTITY_ITEM_EDGE)] += weight

        for entity_index, (left_id, left_weight) in enumerate(entities):
            for right_id, right_weight in entities[entity_index + 1:]:
                pair_weight = min(1.0, (left_weight + right_weight) / 2.0)
                edge_scores[(left_id, right_id, BOOTSTRAP_COOCCURRENCE_EDGE)] += pair_weight
                edge_scores[(right_id, left_id, BOOTSTRAP_COOCCURRENCE_EDGE)] += pair_weight

        if (
            index == 1
            or index % 100 == 0
            or index == total_items
        ):
            emit_progress(
                state="running",
                phase="processing",
                processed=index,
                total=total_items,
                percent=round((index / total_items) * 100, 1) if total_items else 100.0,
                message=f"processing {index:,} / {total_items:,} memories",
                dry_run=bool(dry_run),
                reset=bool(reset),
            )

    edge_rows = [
        (source_id, target_id, min(1.0, math.log1p(weight)), edge_type)
        for (source_id, target_id, edge_type), weight in edge_scores.items()
    ]

    if not dry_run:
        store = CognitiveStore(cognitive_db_path)
        emit_progress(
            state="running",
            phase="writing",
            processed=total_items,
            total=total_items,
            percent=100.0 if total_items else 100.0,
            message="writing cognitive layer state",
            dry_run=False,
            reset=bool(reset),
        )
        if reset:
            store.clear_all()
        else:
            store.clear_bootstrap_state()
        store.bulk_record_activation_events(event_rows)
        store.bulk_upsert_frame_instances(frame_rows)
        store.bulk_upsert_entity_edges(edge_rows)

    summary = {
        "items": len(items),
        "activation_events": len(event_rows),
        "entity_edges": len(edge_rows),
        "frame_instances": len(frame_rows),
    }
    emit_progress(
        state="completed",
        phase="complete",
        processed=summary["items"],
        total=summary["items"],
        percent=100.0,
        message=(
            "dry run complete"
            if dry_run
            else "cognitive bootstrap complete"
        ),
        dry_run=bool(dry_run),
        reset=bool(reset),
        summary=summary,
    )
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap Recurdo's cognitive layer")
    parser.add_argument(
        "--memory-db",
        default=str(Path.home() / ".recurdo" / "memory.db"),
        help="Path to the source memory.db file",
    )
    parser.add_argument(
        "--cognitive-db",
        default=None,
        help="Optional output path for cognitive_layer.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the bootstrap summary without writing to cognitive_layer.db",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear cognitive_layer.db completely before bootstrapping",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    summary = bootstrap_memory_db(
        args.memory_db,
        cognitive_db_path=args.cognitive_db,
        dry_run=bool(args.dry_run),
        reset=bool(args.reset),
    )
    print(
        "Bootstrapped "
        f"{summary['items']} items, "
        f"{summary['entity_edges']} network edges, "
        f"{summary['frame_instances']} frames extracted "
        f"({summary['activation_events']} activation events)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
