from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .. import config
    from .activation import compute_base_level_activation
except ImportError:
    import config
    from cognition.activation import compute_base_level_activation


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        cleaned = str(value).strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class CognitiveStore:
    """
    Additive cognitive-state storage.

    This database is intentionally separate from memory.db so we can grow the
    memory model without destabilizing the live search/indexing path.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or config.COGNITIVE_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def init_db(self) -> None:
        with self.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_weight REAL NOT NULL DEFAULT 1.0,
                    occurred_at TEXT NOT NULL,
                    metadata TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_activation_history_item
                ON activation_history(item_id);

                CREATE INDEX IF NOT EXISTS idx_activation_history_time
                ON activation_history(occurred_at);

                CREATE TABLE IF NOT EXISTS entity_network (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0.0,
                    edge_type TEXT NOT NULL DEFAULT 'co_occurrence',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, target_id, edge_type)
                );

                CREATE INDEX IF NOT EXISTS idx_entity_network_source
                ON entity_network(source_id);

                CREATE INDEX IF NOT EXISTS idx_entity_network_target
                ON entity_network(target_id);

                CREATE TABLE IF NOT EXISTS frame_instances (
                    item_id TEXT PRIMARY KEY,
                    frame_type TEXT NOT NULL,
                    slots TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_frame_instances_type
                ON frame_instances(frame_type);

                CREATE TABLE IF NOT EXISTS script_states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    script_type TEXT NOT NULL,
                    current_scene TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    entities_involved TEXT,
                    status TEXT NOT NULL DEFAULT 'active'
                );

                CREATE INDEX IF NOT EXISTS idx_script_states_script
                ON script_states(script_type, status, last_activity);

                CREATE TABLE IF NOT EXISTS production_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    conditions TEXT,
                    surfaced_items TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_production_log_rule
                ON production_log(rule_id, fired_at);
                """
            )

    def record_activation_event(
        self,
        item_id: str,
        event_type: str,
        *,
        event_weight: float = 1.0,
        occurred_at: datetime | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = json.dumps(metadata or {}, ensure_ascii=True) if metadata else None
        occurred = _normalize_datetime(occurred_at).isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO activation_history (
                    item_id,
                    event_type,
                    event_weight,
                    occurred_at,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_id, event_type, float(event_weight), occurred, payload),
            )

    def bulk_record_activation_events(
        self,
        events: list[tuple[str, str, float, str, str | None]],
    ) -> None:
        if not events:
            return
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO activation_history (
                    item_id,
                    event_type,
                    event_weight,
                    occurred_at,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                events,
            )

    def get_activation_history(self, item_id: str) -> list[tuple[datetime, float]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT occurred_at, event_weight
                FROM activation_history
                WHERE item_id = ?
                ORDER BY occurred_at ASC
                """,
                (item_id,),
            ).fetchall()
        return [
            (_normalize_datetime(str(row["occurred_at"])), float(row["event_weight"] or 0.0))
            for row in rows
        ]

    def get_activation_events(
        self,
        item_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT item_id, event_type, event_weight, occurred_at, metadata
                FROM activation_history
                WHERE item_id = ?
                ORDER BY occurred_at DESC
                LIMIT ?
                """,
                (item_id, limit),
            ).fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            metadata = payload.get("metadata")
            payload["metadata"] = json.loads(str(metadata or "{}")) if metadata else {}
            events.append(payload)
        return events

    def get_activation_histories(
        self,
        item_ids: list[str],
    ) -> dict[str, list[tuple[datetime, float]]]:
        unique_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
        if not unique_ids:
            return {}

        placeholders = ", ".join("?" for _ in unique_ids)
        histories: dict[str, list[tuple[datetime, float]]] = {item_id: [] for item_id in unique_ids}
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT item_id, occurred_at, event_weight
                FROM activation_history
                WHERE item_id IN ({placeholders})
                ORDER BY item_id ASC, occurred_at ASC
                """,
                unique_ids,
            ).fetchall()

        for row in rows:
            item_id = str(row["item_id"])
            histories.setdefault(item_id, []).append(
                (
                    _normalize_datetime(str(row["occurred_at"])),
                    float(row["event_weight"] or 0.0),
                )
            )
        return histories

    def get_base_level_activation(
        self,
        item_id: str,
        *,
        now: datetime | None = None,
        decay: float = 0.5,
    ) -> float:
        return compute_base_level_activation(
            self.get_activation_history(item_id),
            now=now,
            decay=decay,
        )

    def upsert_entity_edge(
        self,
        source_id: str,
        target_id: str,
        *,
        weight: float,
        edge_type: str = "co_occurrence",
    ) -> None:
        now = _utc_now_iso()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO entity_network (
                    source_id,
                    target_id,
                    weight,
                    edge_type,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, edge_type)
                DO UPDATE SET
                    weight = excluded.weight,
                    updated_at = excluded.updated_at
                """,
                (source_id, target_id, float(weight), edge_type, now, now),
            )

    def bulk_upsert_entity_edges(
        self,
        edges: list[tuple[str, str, float, str]],
    ) -> None:
        if not edges:
            return
        now = _utc_now_iso()
        rows = [
            (source_id, target_id, float(weight), edge_type, now, now)
            for source_id, target_id, weight, edge_type in edges
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO entity_network (
                    source_id,
                    target_id,
                    weight,
                    edge_type,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, edge_type)
                DO UPDATE SET
                    weight = excluded.weight,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def get_related_entities(
        self,
        source_id: str,
        *,
        edge_type: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT target_id, weight, edge_type, updated_at
            FROM entity_network
            WHERE source_id = ?
        """
        params: list[Any] = [source_id]
        if edge_type:
            query += " AND edge_type = ?"
            params.append(edge_type)
        query += " ORDER BY weight DESC, updated_at DESC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()

        return [dict(row) for row in rows]

    def get_neighbors(
        self,
        source_id: str,
        *,
        limit: int = 50,
    ) -> list[tuple[str, float]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT target_id, SUM(weight) AS combined_weight
                FROM entity_network
                WHERE source_id = ?
                GROUP BY target_id
                ORDER BY combined_weight DESC, MAX(updated_at) DESC
                LIMIT ?
                """,
                (source_id, limit),
            ).fetchall()
        return [
            (str(row["target_id"]), min(1.0, float(row["combined_weight"] or 0.0)))
            for row in rows
        ]

    def has_activation_history(self, item_id: str) -> bool:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM activation_history
                WHERE item_id = ?
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
        return row is not None

    def upsert_frame_instance(
        self,
        item_id: str,
        frame_type: str,
        slots: dict[str, Any],
        *,
        confidence: float = 0.0,
    ) -> None:
        now = _utc_now_iso()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO frame_instances (
                    item_id,
                    frame_type,
                    slots,
                    confidence,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id)
                DO UPDATE SET
                    frame_type = excluded.frame_type,
                    slots = excluded.slots,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (
                    item_id,
                    frame_type,
                    json.dumps(slots or {}, ensure_ascii=True, sort_keys=True),
                    float(confidence),
                    now,
                    now,
                ),
            )

    def bulk_upsert_frame_instances(
        self,
        frames: list[tuple[str, str, str, float]],
    ) -> None:
        if not frames:
            return
        now = _utc_now_iso()
        rows = [
            (item_id, frame_type, slots_json, float(confidence), now, now)
            for item_id, frame_type, slots_json, confidence in frames
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO frame_instances (
                    item_id,
                    frame_type,
                    slots,
                    confidence,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id)
                DO UPDATE SET
                    frame_type = excluded.frame_type,
                    slots = excluded.slots,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def get_frame_instance(self, item_id: str) -> dict[str, Any] | None:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT item_id, frame_type, slots, confidence, created_at, updated_at
                FROM frame_instances
                WHERE item_id = ?
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["slots"] = json.loads(str(payload.get("slots") or "{}"))
        return payload

    def get_frame_instances(
        self,
        item_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        unique_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
        if not unique_ids:
            return {}

        placeholders = ", ".join("?" for _ in unique_ids)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT item_id, frame_type, slots, confidence, created_at, updated_at
                FROM frame_instances
                WHERE item_id IN ({placeholders})
                """,
                unique_ids,
            ).fetchall()

        frames: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = dict(row)
            payload["slots"] = json.loads(str(payload.get("slots") or "{}"))
            frames[str(payload["item_id"])] = payload
        return frames

    def get_frame(self, item_id: str) -> dict[str, Any] | None:
        return self.get_frame_instance(item_id)

    def list_frame_instances(
        self,
        *,
        frame_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT item_id, frame_type, slots, confidence, created_at, updated_at
            FROM frame_instances
        """
        params: list[Any] = []
        if frame_type:
            query += " WHERE frame_type = ?"
            params.append(frame_type)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()

        frames: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["slots"] = json.loads(str(payload.get("slots") or "{}"))
            frames.append(payload)
        return frames

    def upsert_script_state(
        self,
        script_type: str,
        current_scene: str,
        *,
        started_at: datetime | str | None = None,
        last_activity: datetime | str | None = None,
        entities_involved: list[str] | None = None,
        status: str = "active",
    ) -> None:
        started = _normalize_datetime(started_at).isoformat()
        activity = _normalize_datetime(last_activity).isoformat()
        entities_json = (
            json.dumps(entities_involved, ensure_ascii=True)
            if entities_involved is not None
            else None
        )

        with self.get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM script_states
                WHERE script_type = ? AND status = ?
                ORDER BY last_activity DESC
                LIMIT 1
                """,
                (script_type, status),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO script_states (
                        script_type,
                        current_scene,
                        started_at,
                        last_activity,
                        entities_involved,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (script_type, current_scene, started, activity, entities_json, status),
                )
            else:
                conn.execute(
                    """
                    UPDATE script_states
                    SET current_scene = ?,
                        last_activity = ?,
                        entities_involved = ?,
                        status = ?
                    WHERE id = ?
                    """,
                    (current_scene, activity, entities_json, status, int(existing["id"])),
                )

    def get_script_states(
        self,
        *,
        status: str | None = "active",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, script_type, current_scene, started_at, last_activity, entities_involved, status
            FROM script_states
        """
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY last_activity DESC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()

        states: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            raw_entities = payload.get("entities_involved")
            payload["entities_involved"] = (
                json.loads(str(raw_entities or "[]"))
                if raw_entities
                else []
            )
            states.append(payload)
        return states

    def get_last_production_fire(self, rule_id: str) -> datetime | None:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT fired_at
                FROM production_log
                WHERE rule_id = ?
                ORDER BY fired_at DESC
                LIMIT 1
                """,
                (rule_id,),
            ).fetchone()

        if row is None or not row["fired_at"]:
            return None
        return _normalize_datetime(str(row["fired_at"]))

    def log_production_fire(
        self,
        rule_id: str,
        *,
        fired_at: datetime | str | None = None,
        conditions: dict[str, Any] | None = None,
        surfaced_items: list[dict[str, Any]] | None = None,
    ) -> None:
        timestamp = _normalize_datetime(fired_at).isoformat()
        conditions_json = (
            json.dumps(conditions, ensure_ascii=True, sort_keys=True)
            if conditions is not None
            else None
        )
        surfaced_json = (
            json.dumps(surfaced_items, ensure_ascii=True)
            if surfaced_items is not None
            else None
        )

        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO production_log (rule_id, fired_at, conditions, surfaced_items)
                VALUES (?, ?, ?, ?)
                """,
                (rule_id, timestamp, conditions_json, surfaced_json),
            )

    def get_production_log(
        self,
        *,
        rule_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, rule_id, fired_at, conditions, surfaced_items
            FROM production_log
        """
        params: list[Any] = []
        if rule_id:
            query += " WHERE rule_id = ?"
            params.append(rule_id)
        query += " ORDER BY fired_at DESC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            conditions = payload.get("conditions")
            surfaced = payload.get("surfaced_items")
            payload["conditions"] = json.loads(str(conditions or "{}")) if conditions else {}
            payload["surfaced_items"] = json.loads(str(surfaced or "[]")) if surfaced else []
            events.append(payload)
        return events

    def clear_bootstrap_state(self) -> None:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM activation_history WHERE event_type LIKE 'bootstrap:%'")
            conn.execute("DELETE FROM entity_network")
            conn.execute("DELETE FROM frame_instances")

    def clear_item_knowledge(
        self,
        item_id: str,
        *,
        extra_edge_types: list[str] | None = None,
        clear_activation_history: bool = False,
    ) -> None:
        item_node = f"item:{item_id}"
        edge_types = [edge_type for edge_type in (extra_edge_types or []) if edge_type]
        with self.get_connection() as conn:
            if clear_activation_history:
                conn.execute(
                    """
                    DELETE FROM activation_history
                    WHERE item_id = ?
                    """,
                    (item_id,),
                )
            conn.execute(
                """
                DELETE FROM entity_network
                WHERE source_id = ?
                   OR target_id = ?
                """,
                (item_node, item_node),
            )
            if edge_types:
                placeholders = ", ".join("?" for _ in edge_types)
                conn.execute(
                    f"""
                    DELETE FROM entity_network
                    WHERE edge_type IN ({placeholders})
                    """,
                    edge_types,
                )
            conn.execute(
                """
                DELETE FROM frame_instances
                WHERE item_id = ?
                """,
                (item_id,),
            )

    def clear_all(self) -> None:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM activation_history")
            conn.execute("DELETE FROM entity_network")
            conn.execute("DELETE FROM frame_instances")
            conn.execute("DELETE FROM script_states")
            conn.execute("DELETE FROM production_log")

    def get_counts(self) -> dict[str, int]:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM activation_history) AS activation_events,
                    (SELECT COUNT(*) FROM entity_network) AS entity_edges,
                    (SELECT COUNT(*) FROM frame_instances) AS frame_instances
                """
            ).fetchone()

        return {
            "activation_events": int(row["activation_events"] or 0),
            "entity_edges": int(row["entity_edges"] or 0),
            "frame_instances": int(row["frame_instances"] or 0),
        }


_STORE: CognitiveStore | None = None


def get_store() -> CognitiveStore:
    global _STORE
    if _STORE is None:
        _STORE = CognitiveStore()
    return _STORE


def init_db() -> None:
    get_store().init_db()
