from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .. import config
    from ..cognition.heuristics import is_person_like_entity
except ImportError:
    import config
    from cognition.heuristics import is_person_like_entity


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_ts() -> float:
    return time.time()


def _normalize_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        cleaned = str(value).strip()
        if not cleaned:
            return datetime.now(timezone.utc)
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_entity_key(name: str | None) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(name or "").casefold()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _query_variants(query: str) -> list[str]:
    normalized = normalize_entity_key(query)
    if not normalized:
        return []
    tokens = [token for token in normalized.split() if len(token) >= 3]
    variants = [normalized]
    if len(tokens) > 1:
        variants.extend(tokens)
        for index in range(len(tokens) - 1):
            variants.append(" ".join(tokens[index:index + 2]))
    return [variant for variant in dict.fromkeys(variants) if variant]


GENERIC_COMMUNITY_TOKENS = {
    "a",
    "an",
    "and",
    "book",
    "books",
    "chapter",
    "chapters",
    "class",
    "classes",
    "data",
    "dataset",
    "datasets",
    "document",
    "documents",
    "example",
    "examples",
    "file",
    "files",
    "figure",
    "figures",
    "image",
    "images",
    "index",
    "indices",
    "input",
    "introduction",
    "item",
    "items",
    "iteration",
    "label",
    "matrix",
    "memory",
    "model",
    "models",
    "note",
    "notes",
    "num",
    "output",
    "page",
    "pages",
    "part",
    "pdf",
    "process",
    "processes",
    "result",
    "results",
    "sample",
    "samples",
    "section",
    "slide",
    "slides",
    "table",
    "tables",
    "test",
    "tests",
    "text",
    "tree",
    "version",
}


def is_meaningful_community_entity(name: str | None) -> bool:
    normalized = normalize_entity_key(name)
    if len(normalized) < 4:
        return False
    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return False
    non_numeric_tokens = [token for token in tokens if not token.isdigit()]
    if not non_numeric_tokens:
        return False
    specific_tokens = [
        token
        for token in non_numeric_tokens
        if token not in GENERIC_COMMUNITY_TOKENS and len(token) >= 3
    ]
    if not specific_tokens:
        return False
    return True


class GraphStore:
    """
    Temporal knowledge graph persisted in SQLite.

    This remains the safe default because the shipped desktop backend currently
    runs on Python 3.11. FalkorDBLite is available behind
    GRAPH_STORE_BACKEND=falkor for Python 3.12+ environments.
    """

    backend_name = "sqlite"
    SINGLETON_PREDICATES = {
        "WORKS_ON",
        "HAS_STATUS",
        "IS_EMPLOYED_BY",
        "LOCATED_IN",
        "REPORTS_TO",
        "HAS_PRIORITY",
    }
    SIGNAL_PROMOTION_RELATIONS = {
        "CO_OCCURS_WITH": {
            ("Person", "Person"): "COLLABORATES_WITH",
            "default": "RELATES_TO",
        },
        "WORKS_TOGETHER_ON": {
            "default": "CONTRIBUTED_TO",
        },
        "CO_AUTHORED": {
            ("Person", "Person"): "COLLABORATES_WITH",
            "default": "RELATES_TO",
        },
        "CO_ATTENDED": {
            ("Person", "Person"): "COLLABORATES_WITH",
            "default": "RELATES_TO",
        },
        "REPRESENTS_ORG": {
            "default": "REPRESENTS_ORG",
        },
        "REFERENCES": {
            "default": "RELATES_TO",
        },
    }
    COLLABORATION_SIGNAL_RELATIONS = {
        "CO_OCCURS_WITH",
        "CO_AUTHORED",
        "CO_ATTENDED",
    }

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or config.GRAPH_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._community_cache_key: tuple[Any, ...] | None = None
        self._community_cache_value: list[dict[str, Any]] | None = None
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self) -> None:
        with self.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    canonical_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    attributes TEXT NOT NULL DEFAULT '{}',
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_entities_name
                ON entities(name);

                CREATE INDEX IF NOT EXISTS idx_entities_type
                ON entities(entity_type);

                CREATE TABLE IF NOT EXISTS entity_aliases (
                    normalized_alias TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity
                ON entity_aliases(entity_id);

                CREATE TABLE IF NOT EXISTS episode_mentions (
                    episode_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL DEFAULT 'MENTIONS',
                    mentioned_at TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    PRIMARY KEY (episode_id, entity_id, relation_type),
                    FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_episode_mentions_entity
                ON episode_mentions(entity_id, mentioned_at);

                CREATE INDEX IF NOT EXISTS idx_episode_mentions_episode
                ON episode_mentions(episode_id);

                CREATE TABLE IF NOT EXISTS temporal_facts (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT,
                    episode_id TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(subject_id) REFERENCES entities(id) ON DELETE CASCADE,
                    FOREIGN KEY(object_id) REFERENCES entities(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_temporal_facts_subject
                ON temporal_facts(subject_id, predicate, valid_from);

                CREATE INDEX IF NOT EXISTS idx_temporal_facts_object
                ON temporal_facts(object_id, predicate, valid_from);

                CREATE INDEX IF NOT EXISTS idx_temporal_facts_episode
                ON temporal_facts(episode_id);

                CREATE TABLE IF NOT EXISTS weak_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_a TEXT NOT NULL,
                    entity_b TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    episode_count INTEGER NOT NULL DEFAULT 1,
                    promoted INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_weak_signals_pair
                ON weak_signals(entity_a, entity_b, relation);

                CREATE TABLE IF NOT EXISTS episode_links (
                    source_episode_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    target_episode_id TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source_episode_id, predicate, target_episode_id)
                );

                CREATE INDEX IF NOT EXISTS idx_episode_links_target
                ON episode_links(target_episode_id, predicate);
                """
            )

    def clear_all(self) -> None:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM episode_links")
            conn.execute("DELETE FROM weak_signals")
            conn.execute("DELETE FROM temporal_facts")
            conn.execute("DELETE FROM episode_mentions")
            conn.execute("DELETE FROM entity_aliases")
            conn.execute("DELETE FROM entities")

    def clear_bootstrap_state(self) -> None:
        self.clear_all()

    def _find_existing_entity_id(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        aliases: list[str] | None = None,
    ) -> str | None:
        normalized_candidates = [
            normalized
            for normalized in {
                normalize_entity_key(name),
                *(normalize_entity_key(alias) for alias in (aliases or [])),
            }
            if normalized
        ]
        if not normalized_candidates:
            return None

        placeholders = ", ".join("?" for _ in normalized_candidates)
        row = conn.execute(
            f"""
            SELECT e.id
            FROM entities AS e
            LEFT JOIN entity_aliases AS a
              ON a.entity_id = e.id
            WHERE e.canonical_key IN ({placeholders})
               OR a.normalized_alias IN ({placeholders})
            ORDER BY e.mention_count DESC, e.updated_at DESC
            LIMIT 1
            """,
            (*normalized_candidates, *normalized_candidates),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def _write_aliases(
        self,
        conn: sqlite3.Connection,
        entity_id: str,
        names: list[str],
    ) -> None:
        for alias in names:
            normalized = normalize_entity_key(alias)
            if not normalized:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO entity_aliases (
                    normalized_alias,
                    entity_id,
                    alias
                ) VALUES (?, ?, ?)
                """,
                (normalized, entity_id, str(alias).strip()),
            )

    def _load_entity_row(
        self,
        conn: sqlite3.Connection,
        entity_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT id, canonical_key, name, entity_type, aliases, attributes
            FROM entities
            WHERE id = ?
            """,
            (entity_id,),
        ).fetchone()

    def _refresh_mention_count(
        self,
        conn: sqlite3.Connection,
        entity_id: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM episode_mentions
            WHERE entity_id = ?
            """,
            (entity_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE entities
            SET mention_count = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (int(row["count"] or 0) if row is not None else 0, _utc_now_iso(), entity_id),
        )

    def _merge_entity_records(
        self,
        conn: sqlite3.Connection,
        *,
        target_id: str,
        source_id: str,
        preferred_name: str | None = None,
        preferred_type: str | None = None,
        extra_aliases: list[str] | None = None,
        extra_attributes: dict[str, Any] | None = None,
        canonical_key: str | None = None,
    ) -> str:
        if target_id == source_id:
            return target_id

        target = self._load_entity_row(conn, target_id)
        source = self._load_entity_row(conn, source_id)
        if target is None:
            return source_id
        if source is None:
            return target_id

        target_aliases = json.loads(str(target["aliases"] or "[]"))
        source_aliases = json.loads(str(source["aliases"] or "[]"))
        target_attributes = json.loads(str(target["attributes"] or "{}"))
        source_attributes = json.loads(str(source["attributes"] or "{}"))

        combined_aliases = [
            alias
            for alias in dict.fromkeys(
                [
                    str(target["name"] or "").strip(),
                    str(source["name"] or "").strip(),
                    *(str(alias).strip() for alias in target_aliases),
                    *(str(alias).strip() for alias in source_aliases),
                    *(str(alias).strip() for alias in (extra_aliases or [])),
                ]
            )
            if alias
        ]
        combined_attributes = dict(target_attributes)
        combined_attributes.update(source_attributes)
        combined_attributes.update(extra_attributes or {})

        resolved_name = str(preferred_name or target["name"] or source["name"] or "").strip()
        for candidate in (str(target["name"] or "").strip(), str(source["name"] or "").strip()):
            if len(candidate) > len(resolved_name):
                resolved_name = candidate
        if not resolved_name:
            resolved_name = str(target["name"] or source["name"] or target_id)

        resolved_type = str(preferred_type or target["entity_type"] or source["entity_type"] or "Concept")
        for candidate in (str(target["entity_type"] or "Concept"), str(source["entity_type"] or "Concept")):
            if resolved_type == "Concept" and candidate != "Concept":
                resolved_type = candidate

        conn.execute(
            """
            INSERT INTO episode_mentions (
                episode_id,
                entity_id,
                relation_type,
                mentioned_at,
                confidence
            )
            SELECT
                episode_id,
                ?,
                relation_type,
                mentioned_at,
                confidence
            FROM episode_mentions
            WHERE entity_id = ?
            ON CONFLICT(episode_id, entity_id, relation_type)
            DO UPDATE SET
                mentioned_at = MIN(episode_mentions.mentioned_at, excluded.mentioned_at),
                confidence = MAX(episode_mentions.confidence, excluded.confidence)
            """,
            (target_id, source_id),
        )
        conn.execute("DELETE FROM episode_mentions WHERE entity_id = ?", (source_id,))
        conn.execute(
            "UPDATE temporal_facts SET subject_id = ? WHERE subject_id = ?",
            (target_id, source_id),
        )
        conn.execute(
            "UPDATE temporal_facts SET object_id = ? WHERE object_id = ?",
            (target_id, source_id),
        )
        conn.execute("DELETE FROM entities WHERE id = ?", (source_id,))

        resolved_key = normalize_entity_key(canonical_key or resolved_name)
        if not resolved_key:
            resolved_key = str(target["canonical_key"] or normalize_entity_key(str(target["name"] or "")) or target_id)

        conn.execute(
            """
            UPDATE entities
            SET canonical_key = ?,
                name = ?,
                entity_type = ?,
                aliases = ?,
                attributes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                resolved_key,
                resolved_name,
                resolved_type,
                json.dumps(combined_aliases, ensure_ascii=True),
                json.dumps(combined_attributes, ensure_ascii=True, sort_keys=True),
                _utc_now_iso(),
                target_id,
            ),
        )
        self._write_aliases(conn, target_id, combined_aliases)
        self._refresh_mention_count(conn, target_id)
        return target_id

    def _upsert_entity_conn(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        cleaned_name = " ".join(str(name).split()).strip()
        if not cleaned_name:
            raise ValueError("Entity name is required")

        merged_aliases = [
            alias
            for alias in dict.fromkeys(
                [cleaned_name, *(str(alias).strip() for alias in (aliases or []) if str(alias).strip())]
            )
            if alias
        ]
        now_iso = _utc_now_iso()
        entity_id = self._find_existing_entity_id(
            conn,
            name=cleaned_name,
            aliases=merged_aliases,
        )

        if entity_id is None:
            entity_id = str(uuid.uuid4())
            try:
                conn.execute(
                    """
                    INSERT INTO entities (
                        id,
                        canonical_key,
                        name,
                        entity_type,
                        aliases,
                        attributes,
                        mention_count,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        entity_id,
                        normalize_entity_key(cleaned_name),
                        cleaned_name,
                        entity_type,
                        json.dumps(merged_aliases, ensure_ascii=True),
                        json.dumps(attributes or {}, ensure_ascii=True, sort_keys=True),
                        now_iso,
                        now_iso,
                    ),
                )
            except sqlite3.IntegrityError:
                recovered_id = self._find_existing_entity_id(
                    conn,
                    name=cleaned_name,
                    aliases=merged_aliases,
                )
                if recovered_id is None:
                    raise
                entity_id = recovered_id
                row = self._load_entity_row(conn, entity_id)
                if row is None:
                    raise
            else:
                self._write_aliases(conn, entity_id, merged_aliases)
                return entity_id

        row = self._load_entity_row(conn, entity_id)
        existing_aliases = json.loads(str(row["aliases"] or "[]")) if row else []
        existing_attributes = json.loads(str(row["attributes"] or "{}")) if row else {}

        combined_aliases = [
            alias
            for alias in dict.fromkeys(
                [*(existing_aliases or []), *merged_aliases]
            )
            if str(alias).strip()
        ]
        combined_attributes = dict(existing_attributes)
        combined_attributes.update(attributes or {})

        preferred_name = cleaned_name
        if row is not None and row["name"]:
            existing_name = str(row["name"])
            if len(existing_name) >= len(cleaned_name):
                preferred_name = existing_name

        preferred_type = entity_type
        if row is not None and row["entity_type"] and str(row["entity_type"]) != "Concept":
            preferred_type = str(row["entity_type"])

        preferred_key = normalize_entity_key(preferred_name)
        conflict = conn.execute(
            """
            SELECT id
            FROM entities
            WHERE canonical_key = ?
              AND id != ?
            LIMIT 1
            """,
            (preferred_key, entity_id),
        ).fetchone()
        if conflict is not None:
            return self._merge_entity_records(
                conn,
                target_id=str(conflict["id"]),
                source_id=entity_id,
                preferred_name=preferred_name,
                preferred_type=preferred_type,
                extra_aliases=combined_aliases,
                extra_attributes=combined_attributes,
                canonical_key=preferred_key,
            )

        conn.execute(
            """
            UPDATE entities
            SET canonical_key = ?,
                name = ?,
                entity_type = ?,
                aliases = ?,
                attributes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                normalize_entity_key(preferred_name),
                preferred_name,
                preferred_type,
                json.dumps(combined_aliases, ensure_ascii=True),
                json.dumps(combined_attributes, ensure_ascii=True, sort_keys=True),
                now_iso,
                entity_id,
            ),
        )
        self._write_aliases(conn, entity_id, combined_aliases)
        return entity_id

    def upsert_entity(
        self,
        *,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        with self.get_connection() as conn:
            return self._upsert_entity_conn(
                conn,
                name=name,
                entity_type=entity_type,
                aliases=aliases,
                attributes=attributes,
            )

    def _link_episode_to_entity_conn(
        self,
        conn: sqlite3.Connection,
        *,
        episode_id: str,
        entity_id: str,
        mentioned_at: datetime | str | None = None,
        relation_type: str = "MENTIONS",
        confidence: float = 1.0,
    ) -> None:
        mentioned_iso = _normalize_datetime(mentioned_at).isoformat()
        existing = conn.execute(
            """
            SELECT 1
            FROM episode_mentions
            WHERE episode_id = ? AND entity_id = ? AND relation_type = ?
            """,
            (episode_id, entity_id, relation_type),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO episode_mentions (
                episode_id,
                entity_id,
                relation_type,
                mentioned_at,
                confidence
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(episode_id, entity_id, relation_type)
            DO UPDATE SET
                mentioned_at = MIN(episode_mentions.mentioned_at, excluded.mentioned_at),
                confidence = MAX(episode_mentions.confidence, excluded.confidence)
            """,
            (episode_id, entity_id, relation_type, mentioned_iso, float(confidence)),
        )
        if existing is None:
            conn.execute(
                """
                UPDATE entities
                SET mention_count = mention_count + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (_utc_now_iso(), entity_id),
            )

    def link_episode_to_entity(
        self,
        episode_id: str,
        entity_id: str,
        *,
        mentioned_at: datetime | str | None = None,
        relation_type: str = "MENTIONS",
        confidence: float = 1.0,
    ) -> None:
        with self.get_connection() as conn:
            self._link_episode_to_entity_conn(
                conn,
                episode_id=episode_id,
                entity_id=entity_id,
                mentioned_at=mentioned_at,
                relation_type=relation_type,
                confidence=confidence,
            )

    def _add_temporal_fact_conn(
        self,
        conn: sqlite3.Connection,
        *,
        subject_id: str,
        predicate: str,
        object_id: str,
        valid_from: datetime | str,
        episode_id: str,
        confidence: float = 1.0,
    ) -> str:
        valid_from_iso = _normalize_datetime(valid_from).isoformat()
        now_iso = _utc_now_iso()
        existing = conn.execute(
            """
            SELECT id
            FROM temporal_facts
            WHERE subject_id = ?
              AND predicate = ?
              AND object_id = ?
              AND episode_id = ?
              AND valid_from = ?
            LIMIT 1
            """,
            (subject_id, predicate, object_id, episode_id, valid_from_iso),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE temporal_facts
                SET confidence = MAX(confidence, ?)
                WHERE id = ?
                """,
                (float(confidence), str(existing["id"])),
            )
            return str(existing["id"])
        if predicate in self.SINGLETON_PREDICATES:
            conn.execute(
                """
                UPDATE temporal_facts
                SET valid_until = ?
                WHERE subject_id = ?
                  AND predicate = ?
                  AND valid_until IS NULL
                  AND valid_from <= ?
                """,
                (valid_from_iso, subject_id, predicate, valid_from_iso),
            )

        fact_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO temporal_facts (
                id,
                subject_id,
                predicate,
                object_id,
                valid_from,
                valid_until,
                episode_id,
                confidence,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                fact_id,
                subject_id,
                predicate,
                object_id,
                valid_from_iso,
                episode_id,
                float(confidence),
                now_iso,
            ),
        )
        return fact_id

    def add_temporal_fact(
        self,
        *,
        subject_id: str,
        predicate: str,
        object_id: str,
        valid_from: datetime | str,
        episode_id: str,
        confidence: float = 1.0,
    ) -> str:
        with self.get_connection() as conn:
            return self._add_temporal_fact_conn(
                conn,
                subject_id=subject_id,
                predicate=predicate,
                object_id=object_id,
                valid_from=valid_from,
                episode_id=episode_id,
                confidence=confidence,
            )

    def link_episode_to_episode(
        self,
        source_episode_id: str,
        predicate: str,
        target_episode_id: str,
        *,
        confidence: float = 1.0,
    ) -> None:
        if not source_episode_id or not target_episode_id or source_episode_id == target_episode_id:
            return
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO episode_links (
                    source_episode_id,
                    predicate,
                    target_episode_id,
                    confidence,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_episode_id, predicate, target_episode_id)
                DO UPDATE SET confidence = MAX(episode_links.confidence, excluded.confidence)
                """,
                (
                    str(source_episode_id),
                    str(predicate),
                    str(target_episode_id),
                    float(confidence),
                    _utc_now_iso(),
                ),
            )

    def record_weak_signal(
        self,
        entity_a: str,
        entity_b: str,
        relation: str,
        episode_id: str,
        confidence: float,
    ) -> tuple[int, bool]:
        left = " ".join(str(entity_a or "").split()).strip()
        right = " ".join(str(entity_b or "").split()).strip()
        if not left or not right:
            return 0, False
        if normalize_entity_key(left) > normalize_entity_key(right):
            left, right = right, left
        now_ts = _utc_now_ts()
        episode_token = str(episode_id or "").strip()
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, evidence, episode_count, confidence
                FROM weak_signals
                WHERE entity_a = ? AND entity_b = ? AND relation = ?
                LIMIT 1
                """,
                (left, right, str(relation)),
            ).fetchone()
            if row is None:
                evidence = [episode_token] if episode_token else []
                conn.execute(
                    """
                    INSERT INTO weak_signals (
                        entity_a,
                        entity_b,
                        relation,
                        confidence,
                        evidence,
                        first_seen,
                        last_seen,
                        episode_count,
                        promoted
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0)
                    """,
                    (
                        left,
                        right,
                        str(relation),
                        float(confidence),
                        json.dumps(evidence, ensure_ascii=True),
                        now_ts,
                        now_ts,
                    ),
                )
                episode_count = 1
            else:
                evidence = list(json.loads(str(row["evidence"] or "[]")))
                if episode_token and episode_token not in evidence:
                    evidence.append(episode_token)
                episode_count = len(evidence) or int(row["episode_count"] or 0) or 1
                conn.execute(
                    """
                    UPDATE weak_signals
                    SET confidence = MAX(confidence, ?),
                        evidence = ?,
                        last_seen = ?,
                        episode_count = ?
                    WHERE id = ?
                    """,
                    (
                        float(confidence),
                        json.dumps(evidence, ensure_ascii=True),
                        now_ts,
                        episode_count,
                        int(row["id"]),
                    ),
                )
            should_promote = self._weak_signal_threshold_met(
                str(relation),
                episode_count,
                float(confidence),
            )
            return episode_count, should_promote

    def _weak_signal_threshold_met(
        self,
        relation: str,
        episode_count: int,
        confidence: float,
    ) -> bool:
        if relation in self.COLLABORATION_SIGNAL_RELATIONS:
            return episode_count >= 3
        return episode_count >= 3 or (episode_count >= 2 and confidence >= 0.8)

    def _collaboration_pair_is_credible(
        self,
        left_entity: dict[str, Any] | None,
        right_entity: dict[str, Any] | None,
    ) -> bool:
        for entity in (left_entity, right_entity):
            if entity is None:
                return False
            if str(entity.get("entity_type") or "") != "Person":
                return False
            if not is_person_like_entity(
                str(entity.get("name") or ""),
                aliases=list(entity.get("aliases") or []),
                attributes=dict(entity.get("attributes") or {}),
            ):
                return False
        return True

    def promote_signal(self, signal_id: int) -> str | None:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, entity_a, entity_b, relation, confidence, evidence, episode_count, promoted
                FROM weak_signals
                WHERE id = ?
                LIMIT 1
                """,
                (int(signal_id),),
            ).fetchone()
            if row is None or int(row["promoted"] or 0) == 1:
                return None

            left_name = str(row["entity_a"])
            right_name = str(row["entity_b"])
            left_entity = self.get_entity_by_name(left_name)
            right_entity = self.get_entity_by_name(right_name)
            left_type = str((left_entity or {}).get("entity_type") or "Concept")
            right_type = str((right_entity or {}).get("entity_type") or "Concept")
            relation = str(row["relation"] or "CO_OCCURS_WITH")
            mapping = self.SIGNAL_PROMOTION_RELATIONS.get(relation, {})
            predicate = str(
                mapping.get((left_type, right_type))
                or mapping.get((right_type, left_type))
                or mapping.get("default")
                or relation
            )
            if relation in self.COLLABORATION_SIGNAL_RELATIONS and not self._collaboration_pair_is_credible(
                left_entity,
                right_entity,
            ):
                conn.execute(
                    "UPDATE weak_signals SET promoted = -1 WHERE id = ?",
                    (int(signal_id),),
                )
                return None
            left_id = self._upsert_entity_conn(conn, name=left_name, entity_type=left_type)
            right_id = self._upsert_entity_conn(conn, name=right_name, entity_type=right_type)
            evidence = list(json.loads(str(row["evidence"] or "[]")))
            episode_id = str(evidence[-1]) if evidence else f"signal:{signal_id}"
            fact_id = self._add_temporal_fact_conn(
                conn,
                subject_id=left_id,
                predicate=predicate,
                object_id=right_id,
                valid_from=_utc_now_iso(),
                episode_id=episode_id,
                confidence=max(0.8, float(row["confidence"] or 0.0)),
            )
            conn.execute(
                "UPDATE weak_signals SET promoted = 1 WHERE id = ?",
                (int(signal_id),),
            )
            return fact_id

    def run_promotions(self) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, entity_a, entity_b, relation, episode_count, confidence
                FROM weak_signals
                WHERE promoted = 0
                ORDER BY episode_count DESC, id ASC
                """
            ).fetchall()

        promoted: list[dict[str, Any]] = []
        for row in rows:
            if not self._weak_signal_threshold_met(
                str(row["relation"] or ""),
                int(row["episode_count"] or 0),
                float(row["confidence"] or 0.0),
            ):
                continue
            fact_id = self.promote_signal(int(row["id"]))
            if fact_id:
                promoted.append(
                    {
                        "signal_id": int(row["id"]),
                        "fact_id": str(fact_id),
                        "entity_a": str(row["entity_a"]),
                        "entity_b": str(row["entity_b"]),
                        "relation": str(row["relation"]),
                        "episode_count": int(row["episode_count"] or 0),
                    }
                )
        return promoted

    def delete_low_confidence_collaborations(
        self,
        *,
        min_confidence: float = 0.6,
        require_person_pair: bool = True,
        require_person_like: bool = True,
        execute: bool = True,
    ) -> dict[str, Any]:
        reason_counts: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []

        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.id,
                    f.confidence,
                    f.subject_id,
                    f.object_id,
                    e1.name AS subject_name,
                    e1.entity_type AS subject_type,
                    e1.aliases AS subject_aliases,
                    e1.attributes AS subject_attributes,
                    e2.name AS object_name,
                    e2.entity_type AS object_type,
                    e2.aliases AS object_aliases,
                    e2.attributes AS object_attributes
                FROM temporal_facts f
                JOIN entities e1 ON e1.id = f.subject_id
                JOIN entities e2 ON e2.id = f.object_id
                WHERE f.predicate = 'COLLABORATES_WITH'
                ORDER BY f.confidence ASC, f.id ASC
                """
            ).fetchall()

            for row in rows:
                subject = {
                    "name": str(row["subject_name"] or ""),
                    "entity_type": str(row["subject_type"] or ""),
                    "aliases": json.loads(str(row["subject_aliases"] or "[]")),
                    "attributes": json.loads(str(row["subject_attributes"] or "{}")),
                }
                target = {
                    "name": str(row["object_name"] or ""),
                    "entity_type": str(row["object_type"] or ""),
                    "aliases": json.loads(str(row["object_aliases"] or "[]")),
                    "attributes": json.loads(str(row["object_attributes"] or "{}")),
                }

                reason = None
                confidence = float(row["confidence"] or 0.0)
                if confidence < float(min_confidence):
                    reason = "below_confidence"
                elif require_person_pair and (
                    subject["entity_type"] != "Person" or target["entity_type"] != "Person"
                ):
                    reason = "non_person_pair"
                elif require_person_like and not self._collaboration_pair_is_credible(subject, target):
                    reason = "non_person_like"

                if reason is None:
                    continue

                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                candidates.append(
                    {
                        "id": str(row["id"]),
                        "subject": subject["name"],
                        "object": target["name"],
                        "reason": reason,
                        "confidence": confidence,
                    }
                )

            deleted = 0
            if execute and candidates:
                placeholders = ", ".join("?" for _ in candidates)
                conn.execute(
                    f"DELETE FROM temporal_facts WHERE id IN ({placeholders})",
                    [candidate["id"] for candidate in candidates],
                )
                deleted = len(candidates)
                self._community_cache_key = None
                self._community_cache_value = None

        return {
            "checked": len(rows),
            "candidates": len(candidates),
            "deleted": deleted if execute else 0,
            "reason_counts": reason_counts,
            "sample": candidates[:20],
        }

    def expire_old_signals(self, *, older_than_days: int = 90) -> int:
        cutoff = _utc_now_ts() - (max(1, int(older_than_days)) * 24 * 60 * 60)
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM weak_signals
                WHERE promoted = 0
                  AND last_seen < ?
                """,
                (cutoff,),
            ).fetchone()
            conn.execute(
                """
                DELETE FROM weak_signals
                WHERE promoted = 0
                  AND last_seen < ?
                """,
                (cutoff,),
            )
        return int(row["count"] or 0) if row is not None else 0

    def get_neighbors(
        self,
        entity_id: str,
        *,
        limit: int = 30,
    ) -> list[tuple[str, str, float]]:
        if not entity_id:
            return []
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT neighbor_id, predicate, MAX(weight) AS weight
                FROM (
                    SELECT object_id AS neighbor_id, predicate, confidence AS weight
                    FROM temporal_facts
                    WHERE subject_id = ?
                    UNION ALL
                    SELECT subject_id AS neighbor_id, predicate, confidence AS weight
                    FROM temporal_facts
                    WHERE object_id = ?
                )
                GROUP BY neighbor_id, predicate
                ORDER BY weight DESC, neighbor_id ASC
                LIMIT ?
                """,
                (entity_id, entity_id, max(1, int(limit))),
            ).fetchall()
        return [
            (str(row["neighbor_id"]), str(row["predicate"]), float(row["weight"] or 0.0))
            for row in rows
            if row["neighbor_id"]
        ]

    def get_episodes_mentioning(
        self,
        entity_id: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not entity_id:
            return []
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT episode_id, mentioned_at, confidence
                FROM episode_mentions
                WHERE entity_id = ?
                ORDER BY confidence DESC, mentioned_at DESC
                LIMIT ?
                """,
                (entity_id, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "id": str(row["episode_id"]),
                "mentioned_at": str(row["mentioned_at"]),
                "confidence": float(row["confidence"] or 0.0),
            }
            for row in rows
        ]

    def get_entity_fact_count(self, entity_id: str) -> int:
        if not entity_id:
            return 0
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM temporal_facts
                WHERE subject_id = ? OR object_id = ?
                """,
                (entity_id, entity_id),
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0

    def clear_episode_state(self, episode_id: str) -> None:
        if not episode_id:
            return

        with self.get_connection() as conn:
            mention_rows = conn.execute(
                """
                SELECT DISTINCT entity_id
                FROM episode_mentions
                WHERE episode_id = ?
                """,
                (episode_id,),
            ).fetchall()
            fact_rows = conn.execute(
                """
                SELECT DISTINCT subject_id, object_id
                FROM temporal_facts
                WHERE episode_id = ?
                """,
                (episode_id,),
            ).fetchall()

            affected_entity_ids: set[str] = {
                str(row["entity_id"])
                for row in mention_rows
                if row["entity_id"]
            }
            for row in fact_rows:
                if row["subject_id"]:
                    affected_entity_ids.add(str(row["subject_id"]))
                if row["object_id"]:
                    affected_entity_ids.add(str(row["object_id"]))

            conn.execute(
                """
                DELETE FROM episode_mentions
                WHERE episode_id = ?
                """,
                (episode_id,),
            )
            conn.execute(
                """
                DELETE FROM temporal_facts
                WHERE episode_id = ?
                """,
                (episode_id,),
            )

            now_iso = _utc_now_iso()
            for entity_id in affected_entity_ids:
                mention_count_row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM episode_mentions
                    WHERE entity_id = ?
                    """,
                    (entity_id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE entities
                    SET mention_count = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (int(mention_count_row[0] or 0), now_iso, entity_id),
                )

            orphan_rows = conn.execute(
                """
                SELECT e.id
                FROM entities AS e
                WHERE e.mention_count <= 0
                  AND NOT EXISTS (
                    SELECT 1
                    FROM temporal_facts AS f
                    WHERE f.subject_id = e.id OR f.object_id = e.id
                  )
                """
            ).fetchall()
            if orphan_rows:
                conn.executemany(
                    "DELETE FROM entities WHERE id = ?",
                    [(str(row["id"]),) for row in orphan_rows if row["id"]],
                )
            self._community_cache_key = None
            self._community_cache_value = None

    def get_counts(self) -> dict[str, int]:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM entities) AS entities,
                    (SELECT COUNT(*) FROM entity_aliases) AS aliases,
                    (SELECT COUNT(*) FROM episode_mentions) AS episode_mentions,
                    (SELECT COUNT(*) FROM temporal_facts) AS temporal_facts
                """
            ).fetchone()
        return {
            "entities": int(row["entities"] or 0),
            "aliases": int(row["aliases"] or 0),
            "episode_mentions": int(row["episode_mentions"] or 0),
            "temporal_facts": int(row["temporal_facts"] or 0),
        }

    def _entity_row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        available_keys = set(row.keys())
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "entity_type": str(row["entity_type"]),
            "aliases": json.loads(str(row["aliases"] or "[]")),
            "attributes": json.loads(str(row["attributes"] or "{}")),
            "mention_count": int(row["mention_count"] or 0),
            "created_at": str(row["created_at"]) if "created_at" in available_keys else "",
            "updated_at": str(row["updated_at"]) if "updated_at" in available_keys else "",
        }

    def _search_entity_rows(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        variants = _query_variants(query)
        if not variants:
            return []

        where_clauses: list[str] = []
        params: list[Any] = []
        for variant in variants:
            like_value = f"%{variant}%"
            where_clauses.extend(
                [
                    "e.canonical_key = ?",
                    "lower(e.name) = ?",
                    "a.normalized_alias = ?",
                    "e.canonical_key LIKE ?",
                    "lower(e.name) LIKE ?",
                    "a.normalized_alias LIKE ?",
                ]
            )
            params.extend([variant, variant, variant, like_value, like_value, like_value])

        rows = conn.execute(
            f"""
            SELECT DISTINCT e.*
            FROM entities AS e
            LEFT JOIN entity_aliases AS a
              ON a.entity_id = e.id
            WHERE {' OR '.join(where_clauses)}
            LIMIT ?
            """,
            (*params, max(limit * 5, 25)),
        ).fetchall()
        return list(rows)

    def _score_entity_match(self, query: str, entity: dict[str, Any]) -> float:
        normalized_query = normalize_entity_key(query)
        if not normalized_query:
            return 0.0

        aliases = [normalize_entity_key(alias) for alias in entity.get("aliases", [])]
        fields = [normalize_entity_key(entity["name"]), *aliases]
        query_tokens = set(normalized_query.split())
        if not query_tokens:
            return 0.0

        best = 0.0
        for field in fields:
            if not field:
                continue
            field_tokens = set(field.split())
            token_overlap = len(query_tokens & field_tokens) / max(1, len(query_tokens))
            score = token_overlap * 0.65
            if field == normalized_query:
                score += 1.0
            elif normalized_query in field:
                score += 0.75
            elif field in normalized_query:
                score += 0.55
            best = max(best, score)

        mention_bonus = min(0.25, math.log1p(int(entity.get("mention_count") or 0)) / 10.0)
        return best + mention_bonus

    def get_entity_by_name(self, entity_name: str) -> dict[str, Any] | None:
        normalized = normalize_entity_key(entity_name)
        if not normalized:
            return None
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT e.*
                FROM entities AS e
                LEFT JOIN entity_aliases AS a
                  ON a.entity_id = e.id
                WHERE e.id = ?
                   OR e.canonical_key = ?
                   OR a.normalized_alias = ?
                   OR lower(e.name) = lower(?)
                ORDER BY
                    CASE
                        WHEN e.canonical_key = ? THEN 0
                        WHEN lower(e.name) = lower(?) THEN 1
                        ELSE 2
                    END,
                    e.mention_count DESC,
                    e.updated_at DESC
                LIMIT 1
                """,
                (entity_name, normalized, normalized, entity_name, normalized, entity_name),
            ).fetchone()
            entity = self._entity_row_to_dict(row)
            if entity is not None:
                return entity
            matches = self.search_entities(entity_name, limit=1)
        return matches[0] if matches else None

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        if not entity_id:
            return None
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, name, entity_type, aliases, attributes, mention_count, created_at, updated_at
                FROM entities
                WHERE id = ?
                LIMIT 1
                """,
                (str(entity_id),),
            ).fetchone()
        return self._entity_row_to_dict(row)

    def search_entities(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            rows = self._search_entity_rows(conn, query, limit=limit)

        scored_matches: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            entity = self._entity_row_to_dict(row)
            if entity is None:
                continue
            score = self._score_entity_match(query, entity)
            if score <= 0:
                continue
            entity["match_score"] = round(score, 3)
            scored_matches.append((score, entity))

        scored_matches.sort(
            key=lambda entry: (
                entry[0],
                int(entry[1]["mention_count"]),
                entry[1]["name"],
            ),
            reverse=True,
        )
        return [entity for _, entity in scored_matches[:limit]]

    def get_episode_entities(
        self,
        episode_ids: list[str],
        *,
        limit_per_episode: int = 12,
    ) -> dict[str, list[dict[str, Any]]]:
        unique_episode_ids = [episode_id for episode_id in dict.fromkeys(episode_ids) if episode_id]
        if not unique_episode_ids:
            return {}

        placeholders = ", ".join("?" for _ in unique_episode_ids)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    m.episode_id,
                    m.confidence AS mention_confidence,
                    m.relation_type,
                    m.mentioned_at,
                    e.id,
                    e.name,
                    e.entity_type,
                    e.aliases,
                    e.attributes,
                    e.mention_count,
                    e.created_at,
                    e.updated_at
                FROM episode_mentions AS m
                JOIN entities AS e
                  ON e.id = m.entity_id
                WHERE m.episode_id IN ({placeholders})
                ORDER BY m.episode_id ASC, m.confidence DESC, e.mention_count DESC, e.name ASC
                """,
                unique_episode_ids,
            ).fetchall()

        grouped: dict[str, list[dict[str, Any]]] = {episode_id: [] for episode_id in unique_episode_ids}
        for row in rows:
            episode_id = str(row["episode_id"])
            entity = self._entity_row_to_dict(row)
            if entity is None:
                continue
            entity["mention_confidence"] = float(row["mention_confidence"] or 0.0)
            entity["relation_type"] = str(row["relation_type"])
            entity["mentioned_at"] = str(row["mentioned_at"])
            bucket = grouped.setdefault(episode_id, [])
            if len(bucket) < limit_per_episode:
                bucket.append(entity)
        return grouped

    def get_facts_at_time(
        self,
        entity_id: str,
        point_in_time: datetime | str,
    ) -> list[dict[str, Any]]:
        point_iso = _normalize_datetime(point_in_time).isoformat()
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN f.subject_id = ? THEN 'subject'
                        ELSE 'object'
                    END AS entity_role,
                    subject_entity.name AS subject_name,
                    subject_entity.entity_type AS subject_type,
                    f.predicate,
                    object_entity.name AS object_name,
                    object_entity.entity_type AS object_type,
                    f.valid_from,
                    f.valid_until,
                    f.confidence,
                    f.episode_id
                FROM temporal_facts AS f
                JOIN entities AS subject_entity
                  ON subject_entity.id = f.subject_id
                JOIN entities AS object_entity
                  ON object_entity.id = f.object_id
                WHERE (f.subject_id = ? OR f.object_id = ?)
                  AND f.valid_from <= ?
                  AND (f.valid_until IS NULL OR f.valid_until > ?)
                ORDER BY f.valid_from ASC
                """,
                (entity_id, entity_id, entity_id, point_iso, point_iso),
            ).fetchall()
        return [
            {
                "entity_role": str(row["entity_role"]),
                "subject": str(row["subject_name"]),
                "subject_type": str(row["subject_type"]),
                "predicate": str(row["predicate"]),
                "object": str(row["object_name"]),
                "object_type": str(row["object_type"]),
                "valid_from": str(row["valid_from"]),
                "valid_until": row["valid_until"],
                "confidence": float(row["confidence"] or 0.0),
                "episode_id": str(row["episode_id"]),
            }
            for row in rows
        ]

    def get_entity_timeline(self, entity_name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        entity = self.get_entity_by_name(entity_name)
        if entity is None:
            return []
        entity_id = str(entity["id"])
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    subject_entity.name AS subject_name,
                    f.predicate,
                    object_entity.name AS object_name,
                    f.valid_from,
                    f.valid_until,
                    f.episode_id,
                    f.confidence
                FROM temporal_facts AS f
                JOIN entities AS subject_entity
                  ON subject_entity.id = f.subject_id
                JOIN entities AS object_entity
                  ON object_entity.id = f.object_id
                WHERE f.subject_id = ?
                   OR f.object_id = ?
                ORDER BY f.valid_from ASC, f.created_at ASC
                LIMIT ?
                """,
                (entity_id, entity_id, limit),
            ).fetchall()
        return [
            {
                "subject": str(row["subject_name"]),
                "predicate": str(row["predicate"]),
                "object": str(row["object_name"]),
                "valid_from": str(row["valid_from"]),
                "valid_until": row["valid_until"],
                "episode_id": str(row["episode_id"]),
                "confidence": float(row["confidence"] or 0.0),
                "is_current": row["valid_until"] is None,
            }
            for row in rows
        ]

    def get_episode_mentions(
        self,
        entity_name: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        entity = self.get_entity_by_name(entity_name)
        if entity is None:
            return []
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT episode_id, relation_type, mentioned_at, confidence
                FROM episode_mentions
                WHERE entity_id = ?
                ORDER BY mentioned_at ASC, episode_id ASC
                LIMIT ?
                """,
                (entity["id"], limit),
            ).fetchall()
        return [
            {
                "episode_id": str(row["episode_id"]),
                "relation_type": str(row["relation_type"]),
                "mentioned_at": str(row["mentioned_at"]),
                "confidence": float(row["confidence"] or 0.0),
            }
            for row in rows
        ]

    def get_entities_active_between(
        self,
        start: datetime | str,
        end: datetime | str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        start_iso = _normalize_datetime(start).isoformat()
        end_iso = _normalize_datetime(end).isoformat()
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.id,
                    e.name,
                    e.entity_type,
                    e.aliases,
                    e.attributes,
                    e.mention_count,
                    COUNT(*) AS period_mentions,
                    MAX(m.mentioned_at) AS last_mentioned_at
                FROM episode_mentions AS m
                JOIN entities AS e
                  ON e.id = m.entity_id
                WHERE m.mentioned_at >= ?
                  AND m.mentioned_at <= ?
                GROUP BY e.id, e.name, e.entity_type, e.aliases, e.attributes, e.mention_count
                ORDER BY period_mentions DESC, last_mentioned_at DESC, e.name ASC
                LIMIT ?
                """,
                (start_iso, end_iso, limit),
            ).fetchall()
        entities: list[dict[str, Any]] = []
        for row in rows:
            entity = self._entity_row_to_dict(row)
            if entity is None:
                continue
            entity["period_mentions"] = int(row["period_mentions"] or 0)
            entity["last_mentioned_at"] = str(row["last_mentioned_at"] or "")
            entities.append(entity)
        return entities

    def score_items_for_query_entities(
        self,
        query_entities: list[str],
        item_ids: list[str],
        *,
        limit_per_query: int = 3,
    ) -> dict[str, Any]:
        unique_item_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
        if not unique_item_ids or not query_entities:
            return {
                "scores": {},
                "seed_entities": [],
                "direct_matches": {},
                "neighbor_matches": {},
            }

        seed_entities_by_id: dict[str, dict[str, Any]] = {}
        seed_weights: dict[str, float] = {}
        raw_seed_entities: list[dict[str, Any]] = []

        for raw_query in query_entities:
            cleaned = str(raw_query).removeprefix("entity:").replace("-", " ").replace("_", " ").strip()
            if not cleaned:
                continue
            matches = self.search_entities(cleaned, limit=limit_per_query)
            for rank, entity in enumerate(matches):
                entity_id = str(entity["id"])
                weight = max(0.2, 1.0 - (rank * 0.2))
                match_score = float(entity.get("match_score") or 0.0)
                seed_weights[entity_id] = max(seed_weights.get(entity_id, 0.0), min(1.0, weight * max(0.45, match_score)))
                seed_entities_by_id.setdefault(entity_id, entity)
            if matches:
                raw_seed_entities.append(
                    {
                        "query": cleaned,
                        "matches": [
                            {
                                "id": str(entity["id"]),
                                "name": str(entity["name"]),
                                "entity_type": str(entity["entity_type"]),
                                "match_score": float(entity.get("match_score") or 0.0),
                            }
                            for entity in matches
                        ],
                    }
                )

        if not seed_weights:
            return {
                "scores": {},
                "seed_entities": [],
                "direct_matches": {},
                "neighbor_matches": {},
            }

        episode_entities = self.get_episode_entities(unique_item_ids)
        seed_ids = list(seed_weights)
        placeholders = ", ".join("?" for _ in seed_ids)
        neighbor_weights: dict[str, float] = {}

        with self.get_connection() as conn:
            temporal_rows = conn.execute(
                f"""
                SELECT neighbor_id, SUM(weight) AS total_weight
                FROM (
                    SELECT object_id AS neighbor_id, confidence AS weight
                    FROM temporal_facts
                    WHERE subject_id IN ({placeholders})
                    UNION ALL
                    SELECT subject_id AS neighbor_id, confidence AS weight
                    FROM temporal_facts
                    WHERE object_id IN ({placeholders})
                )
                GROUP BY neighbor_id
                """,
                (*seed_ids, *seed_ids),
            ).fetchall()
            shared_rows = conn.execute(
                f"""
                SELECT m2.entity_id AS neighbor_id, COUNT(*) AS total_weight
                FROM episode_mentions AS m1
                JOIN episode_mentions AS m2
                  ON m1.episode_id = m2.episode_id
                WHERE m1.entity_id IN ({placeholders})
                  AND m2.entity_id NOT IN ({placeholders})
                GROUP BY m2.entity_id
                """,
                (*seed_ids, *seed_ids),
            ).fetchall()

        for row in temporal_rows:
            neighbor_id = str(row["neighbor_id"])
            if neighbor_id in seed_weights:
                continue
            neighbor_weights[neighbor_id] = max(
                neighbor_weights.get(neighbor_id, 0.0),
                min(0.9, 0.25 + (float(row["total_weight"] or 0.0) * 0.35)),
            )
        for row in shared_rows:
            neighbor_id = str(row["neighbor_id"])
            if neighbor_id in seed_weights:
                continue
            neighbor_weights[neighbor_id] = max(
                neighbor_weights.get(neighbor_id, 0.0),
                min(0.8, 0.2 + (float(row["total_weight"] or 0.0) * 0.18)),
            )

        scores: dict[str, float] = {}
        direct_matches: dict[str, list[str]] = {}
        neighbor_matches: dict[str, list[str]] = {}
        for item_id, entities in episode_entities.items():
            direct_names: list[str] = []
            neighbor_names: list[str] = []
            direct_score = 0.0
            neighbor_score = 0.0
            for entity in entities:
                entity_id = str(entity["id"])
                entity_name = str(entity["name"])
                if entity_id in seed_weights:
                    direct_score += seed_weights[entity_id]
                    direct_names.append(entity_name)
                elif entity_id in neighbor_weights:
                    neighbor_score += neighbor_weights[entity_id]
                    neighbor_names.append(entity_name)

            graph_score = min(
                1.0,
                min(1.0, direct_score) * 0.75
                + min(1.0, neighbor_score) * 0.35,
            )
            if graph_score <= 0:
                continue
            scores[item_id] = round(graph_score, 4)
            if direct_names:
                direct_matches[item_id] = list(dict.fromkeys(direct_names))[:5]
            if neighbor_names:
                neighbor_matches[item_id] = list(dict.fromkeys(neighbor_names))[:5]

        return {
            "scores": scores,
            "seed_entities": raw_seed_entities,
            "direct_matches": direct_matches,
            "neighbor_matches": neighbor_matches,
        }

    def detect_communities(
        self,
        *,
        limit: int = 12,
        min_size: int = 3,
        min_shared_mentions: int = 2,
        min_mentions_per_entity: int = 2,
    ) -> list[dict[str, Any]]:
        cache_key = (
            self.db_path.stat().st_mtime_ns if self.db_path.exists() else 0,
            limit,
            min_size,
            min_shared_mentions,
            min_mentions_per_entity,
        )
        if self._community_cache_key == cache_key and self._community_cache_value is not None:
            return [dict(community) for community in self._community_cache_value[:limit]]

        with self.get_connection() as conn:
            entity_rows = conn.execute(
                """
                SELECT id, name, entity_type, aliases, attributes, mention_count, created_at, updated_at
                FROM entities
                WHERE mention_count >= ?
                """,
                (min_mentions_per_entity,),
            ).fetchall()
            entities = {
                str(row["id"]): self._entity_row_to_dict(row)
                for row in entity_rows
                if is_meaningful_community_entity(str(row["name"]))
            }

            co_rows = conn.execute(
                """
                SELECT
                    m1.entity_id AS left_id,
                    m2.entity_id AS right_id,
                    COUNT(DISTINCT m1.episode_id) AS shared_episodes
                FROM episode_mentions AS m1
                JOIN episode_mentions AS m2
                  ON m1.episode_id = m2.episode_id
                 AND m1.entity_id < m2.entity_id
                JOIN entities AS e1
                  ON e1.id = m1.entity_id
                JOIN entities AS e2
                  ON e2.id = m2.entity_id
                WHERE e1.mention_count >= ?
                  AND e2.mention_count >= ?
                GROUP BY m1.entity_id, m2.entity_id
                HAVING shared_episodes >= ?
                """,
                (min_mentions_per_entity, min_mentions_per_entity, min_shared_mentions),
            ).fetchall()
            fact_rows = conn.execute(
                """
                SELECT subject_id AS left_id, object_id AS right_id, COUNT(*) AS fact_count
                FROM temporal_facts
                GROUP BY subject_id, object_id
                """,
            ).fetchall()

            adjacency: dict[str, set[str]] = {}
            edge_weights: dict[tuple[str, str], float] = {}

            def connect(left_id: str, right_id: str, weight: float) -> None:
                if left_id == right_id or left_id not in entities or right_id not in entities:
                    return
                adjacency.setdefault(left_id, set()).add(right_id)
                adjacency.setdefault(right_id, set()).add(left_id)
                edge_key = tuple(sorted((left_id, right_id)))
                edge_weights[edge_key] = max(edge_weights.get(edge_key, 0.0), weight)

            for row in co_rows:
                connect(
                    str(row["left_id"]),
                    str(row["right_id"]),
                    0.4 + (float(row["shared_episodes"] or 0.0) * 0.3),
                )
            for row in fact_rows:
                connect(
                    str(row["left_id"]),
                    str(row["right_id"]),
                    0.8 + (float(row["fact_count"] or 0.0) * 0.35),
                )

            communities: list[dict[str, Any]] = []
            visited: set[str] = set()
            for entity_id in sorted(adjacency):
                if entity_id in visited:
                    continue
                component: list[str] = []
                queue: deque[str] = deque([entity_id])
                visited.add(entity_id)
                while queue:
                    current = queue.popleft()
                    component.append(current)
                    for neighbor_id in adjacency.get(current, set()):
                        if neighbor_id in visited:
                            continue
                        visited.add(neighbor_id)
                        queue.append(neighbor_id)

                if len(component) < min_size:
                    continue

                member_entities = [
                    entities[current_id]
                    for current_id in component
                    if current_id in entities and entities[current_id] is not None
                ]
                member_entities.sort(
                    key=lambda entity: (
                        int(entity["mention_count"]),
                        entity["name"],
                    ),
                    reverse=True,
                )

                component_set = set(component)
                community_episode_rows = conn.execute(
                    f"""
                    SELECT DISTINCT episode_id
                    FROM episode_mentions
                    WHERE entity_id IN ({", ".join("?" for _ in component)})
                    """,
                    component,
                ).fetchall()
                episode_ids = [str(row["episode_id"]) for row in community_episode_rows]
                last_activity_row = conn.execute(
                    f"""
                    SELECT MAX(mentioned_at) AS last_activity
                    FROM episode_mentions
                    WHERE entity_id IN ({", ".join("?" for _ in component)})
                    """,
                    component,
                ).fetchone()
                edge_count = sum(1 for edge in edge_weights if edge[0] in component_set and edge[1] in component_set)
                activity_score = min(
                    1.0,
                    (
                        sum(int(entity["mention_count"]) for entity in member_entities)
                        / max(1, len(member_entities) * 10)
                    ),
                )

                type_priority = {
                    "Project": 0,
                    "Concept": 1,
                    "Document": 2,
                    "Organisation": 3,
                    "Person": 4,
                }
                label_candidates = sorted(
                    member_entities,
                    key=lambda entity: (
                        type_priority.get(str(entity["entity_type"]), 5),
                        -int(entity["mention_count"]),
                        -len(normalize_entity_key(entity["name"])),
                        str(entity["name"]),
                    ),
                )
                representative_names = [
                    str(entity["name"])
                    for entity in label_candidates
                    if is_meaningful_community_entity(str(entity["name"]))
                ][:4]
                label_root = representative_names[0] if representative_names else "Memory"
                top_entities = [
                    {
                        "id": str(entity["id"]),
                        "name": str(entity["name"]),
                        "entity_type": str(entity["entity_type"]),
                        "mention_count": int(entity["mention_count"]),
                    }
                    for entity in member_entities
                    if is_meaningful_community_entity(str(entity["name"]))
                ][:6]
                if len(top_entities) < min_size:
                    continue
                community_id = "community:" + hashlib.sha1(
                    "|".join(sorted(component)).encode("utf-8")
                ).hexdigest()[:12]
                communities.append(
                    {
                        "id": community_id,
                        "label": f"{label_root} Cluster",
                        "entity_count": len(member_entities),
                        "edge_count": edge_count,
                        "episode_count": len(episode_ids),
                        "activity_score": round(activity_score, 3),
                        "last_activity": str(last_activity_row["last_activity"] or "") if last_activity_row is not None else "",
                        "top_entities": top_entities,
                        "sample_episode_ids": episode_ids[:8],
                        "member_entity_ids": sorted(component),
                    }
                )

        communities.sort(
            key=lambda community: (
                float(community["activity_score"]),
                int(community["episode_count"]),
                int(community["entity_count"]),
            ),
            reverse=True,
        )
        self._community_cache_key = cache_key
        self._community_cache_value = [dict(community) for community in communities]
        return [dict(community) for community in communities[:limit]]

    def get_community(self, community_id: str) -> dict[str, Any] | None:
        for community in self.detect_communities(limit=100):
            if str(community.get("id")) == str(community_id):
                return community
        return None

    def find_connections(self, entity_id: str, *, depth: int = 2, limit: int = 50) -> list[dict[str, Any]]:
        target = self.get_entity_by_name(entity_id)
        if target is None:
            return []

        start_id = str(target["id"])
        visited = {start_id}
        frontier: deque[tuple[str, int]] = deque([(start_id, 0)])
        discovered: dict[str, int] = {}

        with self.get_connection() as conn:
            while frontier:
                current_id, current_depth = frontier.popleft()
                if current_depth >= depth:
                    continue

                temporal_rows = conn.execute(
                    """
                    SELECT DISTINCT neighbor_id
                    FROM (
                        SELECT object_id AS neighbor_id
                        FROM temporal_facts
                        WHERE subject_id = ?
                        UNION
                        SELECT subject_id AS neighbor_id
                        FROM temporal_facts
                        WHERE object_id = ?
                    )
                    """,
                    (current_id, current_id),
                ).fetchall()
                shared_episode_rows = conn.execute(
                    """
                    SELECT DISTINCT m2.entity_id AS neighbor_id
                    FROM episode_mentions AS m1
                    JOIN episode_mentions AS m2
                      ON m1.episode_id = m2.episode_id
                    WHERE m1.entity_id = ?
                      AND m2.entity_id <> ?
                    """,
                    (current_id, current_id),
                ).fetchall()

                for row in list(temporal_rows) + list(shared_episode_rows):
                    neighbor_id = str(row["neighbor_id"])
                    if neighbor_id in visited:
                        continue
                    visited.add(neighbor_id)
                    discovered[neighbor_id] = current_depth + 1
                    frontier.append((neighbor_id, current_depth + 1))
                    if len(discovered) >= limit:
                        frontier.clear()
                        break

            if not discovered:
                return []

            placeholders = ", ".join("?" for _ in discovered)
            rows = conn.execute(
                f"""
                SELECT id, name, entity_type, aliases, attributes, mention_count, created_at, updated_at
                FROM entities
                WHERE id IN ({placeholders})
                """,
                list(discovered),
            ).fetchall()

        connections: list[dict[str, Any]] = []
        for row in rows:
            entity = self._entity_row_to_dict(row)
            if entity is None:
                continue
            entity["hops"] = discovered[str(row["id"])]
            connections.append(entity)

        connections.sort(key=lambda entity: (int(entity["hops"]), -int(entity["mention_count"]), entity["name"]))
        return connections[:limit]


def create_store(
    db_path: str | Path | None = None,
    *,
    backend: str | None = None,
) -> Any:
    requested_backend = (backend or getattr(config, "GRAPH_STORE_BACKEND", "sqlite")).strip().lower()
    if requested_backend in {"falkor", "falkordb", "falkordblite"}:
        try:
            from .falkor_store import FalkorGraphStore

            return FalkorGraphStore(db_path or config.GRAPH_DB_PATH)
        except Exception as exc:
            if getattr(config, "GRAPH_STORE_STRICT", False):
                raise
            import logging

            logging.getLogger(__name__).warning(
                "Falkor graph backend unavailable (%s); falling back to SQLite graph store",
                exc,
            )
    return GraphStore(db_path or config.GRAPH_DB_PATH)


_store: Any | None = None


def get_store() -> Any:
    global _store
    if _store is None:
        _store = create_store()
    return _store
