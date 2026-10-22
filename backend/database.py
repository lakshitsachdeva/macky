from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from . import config
except ImportError:
    import config


DB_PATH = config.DB_PATH
RETRYABLE_DB_ERROR_MARKERS = (
    "database is locked",
    "database schema is locked",
    "database table is locked",
)


ITEM_COLUMNS = {
    "id",
    "content_hash",
    "title",
    "filename",
    "file_type",
    "handler_type",
    "source_type",
    "original_path",
    "created_at",
    "modified_at",
    "first_seen_at",
    "file_mtime",
    "file_size_bytes",
    "last_opened_at",
    "sender_name",
    "sender_email",
    "extracted_text",
    "summary",
    "keywords",
    "doc_type_guess",
    "importance_score",
    "open_count",
    "total_dwell_seconds",
    "copy_event_count",
    "preview_text",
    "enrichment_tier",
    "ocr_pending",
    "is_excluded",
}

FTS_INDEX_COLUMNS = (
    "title",
    "filename",
    "preview_text",
    "original_path",
    "extracted_text",
    "summary",
    "keywords",
)

EMBEDDING_COLUMNS = {
    "item_id",
    "embedding_type",
    "faiss_id",
    "sparse_weights",
    "model_name",
    "created_at",
}

EVENT_COLUMNS = {
    "id",
    "item_id",
    "event_type",
    "occurred_at",
    "duration_seconds",
    "metadata",
}

RELATIONSHIP_COLUMNS = {
    "id",
    "from_item_id",
    "to_item_id",
    "relationship_type",
    "created_at",
}

WATCHED_FOLDER_COLUMNS = {
    "path",
    "label",
    "added_at",
    "is_default",
}

WATCHED_FILE_COLUMNS = {
    "path",
    "item_id",
    "last_seen",
    "file_size",
    "status",
    "added_at",
    "error_count",
    "last_error",
    "last_attempted",
}

CASE_COLUMNS = {
    "id",
    "name",
    "description",
    "status",
    "created_at",
    "updated_at",
    "notes",
}

CASE_EVIDENCE_COLUMNS = {
    "case_id",
    "item_id",
    "source_path",
    "sha256",
    "size_bytes",
    "mtime_ns",
    "imported_at",
    "acquisition_status",
}

CUSTODY_EVENT_COLUMNS = {
    "id",
    "case_id",
    "item_id",
    "event_type",
    "actor",
    "occurred_at",
    "details_json",
    "previous_event_hash",
    "event_hash",
}

MACOS_DATALESS_FLAG = 0x40000000
DATALLESS_FILE_ERROR = "File is not downloaded locally yet"
MAX_WATCHED_FILE_ERRORS = 3
WATCHED_FILE_RETRY_DELAY_SECONDS = 30 * 60


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    """Serialize structured custody data with one stable JSON representation."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def custody_event_hash_payload(
    *,
    event_id: str,
    case_id: str,
    item_id: str | None,
    event_type: str,
    actor: str,
    occurred_at: str,
    details_json: str,
    previous_event_hash: str | None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "case_id": case_id,
        "item_id": item_id,
        "event_type": event_type,
        "actor": actor,
        "occurred_at": occurred_at,
        "details_json": details_json,
        "previous_event_hash": previous_event_hash,
    }


def compute_custody_event_hash(event: dict[str, Any]) -> str:
    payload = custody_event_hash_payload(
        event_id=str(event.get("id") or ""),
        case_id=str(event.get("case_id") or ""),
        item_id=str(event["item_id"]) if event.get("item_id") is not None else None,
        event_type=str(event.get("event_type") or ""),
        actor=str(event.get("actor") or ""),
        occurred_at=str(event.get("occurred_at") or ""),
        details_json=str(event.get("details_json") or "{}"),
        previous_event_hash=(
            str(event["previous_event_hash"])
            if event.get("previous_event_hash") is not None
            else None
        ),
    )
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _normalize_path(path: str | Path) -> str:
    raw = str(path)
    if "://" in raw:
        return raw.strip()
    return str(Path(path).expanduser().resolve(strict=False))


def _path_lookup_values(path: str | Path) -> tuple[str, ...]:
    normalized = _normalize_path(path)
    expanded = str(Path(path).expanduser())
    if normalized == expanded:
        return (normalized,)
    return (normalized, expanded)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _retire_item_ids(conn: sqlite3.Connection, item_ids: list[str]) -> int:
    unique_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
    if not unique_ids:
        return 0

    rows = [(item_id,) for item_id in unique_ids]
    conn.executemany("DELETE FROM embeddings WHERE item_id = ?", rows)
    conn.executemany(
        "UPDATE items SET is_excluded = 1 WHERE id = ?",
        rows,
    )
    return len(unique_ids)


def _retire_items_for_paths(
    conn: sqlite3.Connection,
    paths: list[str | Path],
) -> int:
    if not paths:
        return 0

    lookup_values: list[str] = []
    for path in paths:
        lookup_values.extend(_path_lookup_values(path))

    unique_values = [value for value in dict.fromkeys(lookup_values) if value]
    if not unique_values:
        return 0

    placeholders = ", ".join("?" for _ in unique_values)
    rows = conn.execute(
        f"""
        SELECT id
        FROM items
        WHERE original_path IN ({placeholders})
          AND COALESCE(is_excluded, 0) = 0
        """,
        unique_values,
    ).fetchall()
    return _retire_item_ids(conn, [str(row["id"]) for row in rows])


def _retire_items_under_root(
    conn: sqlite3.Connection,
    root_path: str | Path,
) -> int:
    normalized_root = _normalize_path(root_path)
    rows = conn.execute(
        """
        SELECT id
        FROM items
        WHERE COALESCE(is_excluded, 0) = 0
          AND (
            original_path = ?
            OR original_path LIKE ?
          )
        """,
        (normalized_root, f"{normalized_root}/%"),
    ).fetchall()
    return _retire_item_ids(conn, [str(row["id"]) for row in rows])


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def _validate_columns(payload: dict[str, Any], allowed_columns: set[str]) -> None:
    unknown_columns = sorted(set(payload) - allowed_columns)
    if unknown_columns:
        raise ValueError(f"Unknown columns: {', '.join(unknown_columns)}")


def _print_db_write(label: str, target: str, elapsed: float) -> None:
    print(
        f"[DB_WRITE] {label:<14} {target[:40]:<40} | {elapsed * 1000:.1f}ms"
    )


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Durability + speed tuning for a local-first single-user app.
    # WAL keeps readers unblocked by writers; synchronous=NORMAL fsyncs once per
    # transaction instead of per statement (~20% faster writes) and is safe
    # under WAL for this class of workload. cache_size is 128MB
    # (sized so 10k-100k item deployments fit in RAM). mmap_size enables
    # zero-copy reads for large scans. temp_store=MEMORY keeps sort/join
    # scratch off disk. wal_autocheckpoint caps WAL bloat at ~4MB.
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA cache_size = -131072;")
    conn.execute("PRAGMA mmap_size = 268435456;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA wal_autocheckpoint = 1000;")
    return conn


def is_retryable_operational_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in RETRYABLE_DB_ERROR_MARKERS)


def run_with_connection_retry(callback: Any) -> Any:
    conn = get_connection()
    try:
        return callback(conn)
    except sqlite3.OperationalError as exc:
        if not is_retryable_operational_error(exc):
            raise
        conn.close()
        conn = get_connection()
        return callback(conn)
    finally:
        conn.close()


def _ensure_items_columns(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(items)").fetchall()
    }

    if "file_mtime" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN file_mtime REAL")

    if "file_size_bytes" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN file_size_bytes INTEGER")

    if "ocr_pending" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN ocr_pending INTEGER DEFAULT 0")


def _ensure_watched_files_columns(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(watched_files)").fetchall()
    }

    if "error_count" not in columns:
        conn.execute(
            "ALTER TABLE watched_files ADD COLUMN error_count INTEGER DEFAULT 0"
        )

    if "last_error" not in columns:
        conn.execute("ALTER TABLE watched_files ADD COLUMN last_error TEXT")

    if "last_attempted" not in columns:
        conn.execute("ALTER TABLE watched_files ADD COLUMN last_attempted REAL")


def _ensure_items_fts_schema(conn: sqlite3.Connection) -> None:
    """Keep the external-content FTS table compatible across schema versions."""
    item_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(items)").fetchall()
    }
    index_columns = [
        column for column in FTS_INDEX_COLUMNS if column in item_columns
    ]
    if not index_columns:
        return

    fts_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items_fts'"
    ).fetchone() is not None
    current_columns = (
        [
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(items_fts)").fetchall()
        ]
        if fts_exists
        else []
    )
    needs_rebuild = current_columns != index_columns

    conn.executescript(
        """
        DROP TRIGGER IF EXISTS items_ai;
        DROP TRIGGER IF EXISTS items_au;
        DROP TRIGGER IF EXISTS items_ad;
        """
    )

    if needs_rebuild:
        conn.execute("DROP TABLE IF EXISTS items_fts")
        columns_sql = ",\n                ".join(index_columns)
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE items_fts
            USING fts5(
                {columns_sql},
                content='items',
                content_rowid='rowid',
                tokenize='porter unicode61'
            )
            """
        )

    column_list = ", ".join(index_columns)
    new_values = ", ".join(f"new.{column}" for column in index_columns)
    old_values = ", ".join(f"old.{column}" for column in index_columns)
    conn.executescript(
        f"""
        CREATE TRIGGER items_ai
        AFTER INSERT ON items
        BEGIN
            INSERT INTO items_fts(rowid, {column_list})
            VALUES (new.rowid, {new_values});
        END;

        CREATE TRIGGER items_au
        AFTER UPDATE ON items
        BEGIN
            INSERT INTO items_fts(items_fts, rowid, {column_list})
            VALUES ('delete', old.rowid, {old_values});
            INSERT INTO items_fts(rowid, {column_list})
            VALUES (new.rowid, {new_values});
        END;

        CREATE TRIGGER items_ad
        AFTER DELETE ON items
        BEGIN
            INSERT INTO items_fts(items_fts, rowid, {column_list})
            VALUES ('delete', old.rowid, {old_values});
        END;
        """
    )

    if needs_rebuild:
        conn.execute(
            f"""
            INSERT INTO items_fts(rowid, {column_list})
            SELECT rowid, {column_list}
            FROM items
            """
        )


def _ensure_embeddings_schema(conn: sqlite3.Connection) -> None:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'"
    ).fetchone()
    if table_exists is None:
        return

    pk_columns = [
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
        if int(row["pk"])
    ]
    if pk_columns != ["item_id", "embedding_type"]:
        return

    conn.executescript(
        """
        ALTER TABLE embeddings RENAME TO embeddings_legacy;

        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            embedding_type TEXT NOT NULL,
            faiss_id INTEGER,
            sparse_weights TEXT,
            model_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        INSERT INTO embeddings (
            item_id,
            embedding_type,
            faiss_id,
            sparse_weights,
            model_name,
            created_at
        )
        SELECT
            item_id,
            embedding_type,
            faiss_id,
            sparse_weights,
            model_name,
            created_at
        FROM embeddings_legacy;

        DROP TABLE embeddings_legacy;
        """
    )


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items(content_hash);
        CREATE INDEX IF NOT EXISTS idx_items_source_type ON items(source_type);
        CREATE INDEX IF NOT EXISTS idx_items_file_type ON items(file_type);
        CREATE INDEX IF NOT EXISTS idx_items_handler_type ON items(handler_type);
        CREATE INDEX IF NOT EXISTS idx_items_first_seen_at ON items(first_seen_at);
        CREATE INDEX IF NOT EXISTS idx_items_created_at ON items(created_at);
        CREATE INDEX IF NOT EXISTS idx_items_modified_at ON items(modified_at);
        CREATE INDEX IF NOT EXISTS idx_items_last_opened_at ON items(last_opened_at);
        CREATE INDEX IF NOT EXISTS idx_items_enrichment_tier ON items(enrichment_tier);
        CREATE INDEX IF NOT EXISTS idx_items_is_excluded ON items(is_excluded);
        CREATE INDEX IF NOT EXISTS idx_items_original_path_mtime
        ON items(original_path, file_mtime, file_size_bytes);
        CREATE INDEX IF NOT EXISTS idx_events_item_id ON events(item_id);
        CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
        -- interaction_events: three access patterns —
        --   (a) "what did this user do recently?"  → (user_id, created_at)
        --   (b) "what did this user do with this item?" → (user_id, item_id, event_type)
        --   (c) "what did this user click for this query?" → (user_id, query_hash)
        CREATE INDEX IF NOT EXISTS idx_interaction_events_user_time
        ON interaction_events(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_interaction_events_user_item
        ON interaction_events(user_id, item_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_interaction_events_user_query
        ON interaction_events(user_id, query_hash);
        CREATE INDEX IF NOT EXISTS idx_embeddings_item_id ON embeddings(item_id);
        CREATE INDEX IF NOT EXISTS idx_embeddings_faiss_id ON embeddings(faiss_id);
        CREATE INDEX IF NOT EXISTS idx_watched_files_item_id ON watched_files(item_id);
        CREATE INDEX IF NOT EXISTS idx_watched_files_status ON watched_files(status);
        CREATE INDEX IF NOT EXISTS idx_watched_files_pending_retry
        ON watched_files(status, last_attempted)
        WHERE status = 'pending';
        CREATE INDEX IF NOT EXISTS idx_case_evidence_case_id
        ON case_evidence(case_id);
        CREATE INDEX IF NOT EXISTS idx_case_evidence_item_id
        ON case_evidence(item_id);
        CREATE INDEX IF NOT EXISTS idx_custody_events_case_time
        ON custody_events(case_id, occurred_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_single_non_dense
        ON embeddings(item_id, embedding_type)
        WHERE embedding_type IN ('sparse_json', 'visual');
        """
    )


def fix_existing_timestamps(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    already = conn.execute(
        "SELECT value FROM kv_store WHERE key = 'timestamps_fixed'"
    ).fetchone()
    if already:
        return

    try:
        from .indexer.pipeline import get_file_timestamps
    except ImportError:
        from indexer.pipeline import get_file_timestamps

    rows = conn.execute("SELECT id, original_path FROM items").fetchall()

    fixed = 0
    for row in rows:
        path = Path(str(row["original_path"]))
        if not path.exists():
            continue

        try:
            timestamps = get_file_timestamps(path)
            conn.execute(
                """
                UPDATE items
                SET created_at = ?,
                    modified_at = ?,
                    first_seen_at = ?,
                    file_mtime = ?,
                    file_size_bytes = ?
                WHERE id = ?
                """,
                (
                    timestamps["created_at"],
                    timestamps["modified_at"],
                    timestamps["first_seen_at"],
                    timestamps["file_mtime"],
                    timestamps["file_size_bytes"],
                    row["id"],
                ),
            )
            fixed += 1
        except Exception:
            continue

    conn.commit()
    conn.execute(
        "INSERT OR REPLACE INTO kv_store (key, value) VALUES ('timestamps_fixed', '1')"
    )
    conn.commit()
    print(f"Fixed timestamps for {fixed} existing items")


def backfill_watched_files(conn: sqlite3.Connection) -> int:
    item_rows = conn.execute(
        """
        SELECT id, original_path
        FROM items
        WHERE COALESCE(is_excluded, 0) = 0
          AND original_path IS NOT NULL
          AND TRIM(original_path) <> ''
        """
    ).fetchall()
    watched_paths = {
        str(row["path"])
        for row in conn.execute(
            """
            SELECT path
            FROM watched_files
            WHERE path IS NOT NULL
              AND TRIM(path) <> ''
            """
        ).fetchall()
    }

    orphaned: list[tuple[str, str]] = []
    for row in item_rows:
        item_id = str(row["id"])
        path_str = str(row["original_path"])
        lookup_values = _path_lookup_values(path_str)
        if any(value in watched_paths for value in lookup_values):
            continue
        orphaned.append((item_id, path_str))

    print(f"[DB] Backfilling watched_files for {len(orphaned)} orphaned items")

    inserted = 0
    for item_id, path_str in orphaned:
        path = Path(path_str).expanduser()
        if not path.exists():
            continue

        try:
            stat_result = path.stat()
        except (OSError, PermissionError):
            continue

        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO watched_files (path, item_id, last_seen, file_size, status, added_at)
            VALUES (?, ?, ?, ?, 'indexed', ?)
            """,
            (
                _normalize_path(path),
                item_id,
                float(stat_result.st_mtime),
                int(stat_result.st_size),
                _utc_now_iso(),
            ),
        )
        if conn.total_changes > before:
            inserted += 1

    conn.commit()
    print(f"[DB] Backfilled {inserted} watched_files rows")
    return inserted


def retire_missing_local_items(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT id, original_path
        FROM items
        WHERE COALESCE(is_excluded, 0) = 0
          AND COALESCE(source_type, 'local') = 'local'
          AND original_path IS NOT NULL
          AND TRIM(original_path) <> ''
        """
    ).fetchall()

    missing_ids: list[str] = []
    for row in rows:
        original_path = str(row["original_path"] or "").strip()
        if not original_path:
            continue
        if Path(original_path).expanduser().exists():
            continue
        missing_ids.append(str(row["id"]))

    retired = _retire_item_ids(conn, missing_ids)
    if retired:
        print(f"[DB] Retired {retired} missing local items")
    return retired


def reconcile_pending_files(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT id, path, file_size, last_attempted, error_count, status
        FROM watched_files
        WHERE status IN ('pending', 'error')
        """
    ).fetchall()

    counts = {
        "total": len(rows),
        "still_pending": 0,
        "marked_missing": 0,
        "marked_failed": 0,
        "too_old": 0,
    }
    now = time.time()

    for row in rows:
        path_str = str(row["path"] or "").strip()
        if not path_str:
            conn.execute(
                "UPDATE watched_files SET status = 'failed', last_error = ? WHERE id = ?",
                ("Missing path for pending watched file", row["id"]),
            )
            counts["marked_failed"] += 1
            continue

        path = Path(path_str).expanduser()
        try:
            stat_result = path.stat()
        except (OSError, PermissionError):
            conn.execute(
                """
                UPDATE watched_files
                SET status = 'missing',
                    last_error = COALESCE(last_error, 'File missing at startup')
                WHERE id = ?
                """,
                (row["id"],),
            )
            counts["marked_missing"] += 1
            continue

        if getattr(stat_result, "st_flags", 0) & MACOS_DATALESS_FLAG:
            mark_watched_file_dataless(
                conn,
                path,
                last_seen=float(stat_result.st_mtime),
                file_size=int(stat_result.st_size),
                attempted_at=row["last_attempted"],
            )
            counts["marked_failed"] += 1
            continue

        if path.suffix.lower() not in config.SUPPORTED_IMAGE_EXTENSIONS:
            age_days = (now - float(stat_result.st_mtime)) / 86400
            if age_days > config.MAX_FILE_AGE_DAYS:
                conn.execute(
                    """
                    UPDATE watched_files
                    SET status = 'skipped_age',
                        last_seen = ?,
                        file_size = ?,
                        last_error = COALESCE(last_error, 'Skipped at startup due to age window')
                    WHERE id = ?
                    """,
                    (
                        float(stat_result.st_mtime),
                        int(stat_result.st_size),
                        row["id"],
                    ),
                )
                counts["too_old"] += 1
                continue

        error_count = int(row["error_count"] or 0)
        if error_count >= MAX_WATCHED_FILE_ERRORS:
            conn.execute(
                """
                UPDATE watched_files
                SET status = 'failed',
                    last_seen = ?,
                    file_size = ?
                WHERE id = ?
                """,
                (
                    float(stat_result.st_mtime),
                    int(stat_result.st_size),
                    row["id"],
                ),
            )
            counts["marked_failed"] += 1
            continue

        conn.execute(
            """
            UPDATE watched_files
            SET status = 'pending',
                last_seen = ?,
                file_size = ?
            WHERE id = ?
            """,
            (
                float(stat_result.st_mtime),
                int(stat_result.st_size),
                row["id"],
            ),
        )
        counts["still_pending"] += 1

    conn.commit()
    print(f"[RECONCILE] {counts['total']} pending files reviewed:")
    print(f"[RECONCILE]   still pending:  {counts['still_pending']}")
    print(f"[RECONCILE]   marked missing: {counts['marked_missing']}")
    print(f"[RECONCILE]   too old:        {counts['too_old']}")
    print(f"[RECONCILE]   marked failed:  {counts['marked_failed']}")
    return counts


def init_db() -> None:
    conn = get_connection()

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                content_hash TEXT UNIQUE NOT NULL,
                title TEXT,
                filename TEXT NOT NULL,
                file_type TEXT,
                handler_type TEXT,
                source_type TEXT DEFAULT 'local',
                original_path TEXT NOT NULL,
                created_at TEXT,
                modified_at TEXT,
                first_seen_at TEXT NOT NULL,
                file_mtime REAL,
                file_size_bytes INTEGER,
                last_opened_at TEXT,
                sender_name TEXT,
                sender_email TEXT,
                extracted_text TEXT,
                summary TEXT,
                keywords TEXT DEFAULT '[]',
                doc_type_guess TEXT,
                importance_score REAL DEFAULT 0.5,
                open_count INTEGER DEFAULT 0,
                total_dwell_seconds INTEGER DEFAULT 0,
                copy_event_count INTEGER DEFAULT 0,
                preview_text TEXT,
                enrichment_tier INTEGER DEFAULT 0,
                ocr_pending INTEGER DEFAULT 0,
                is_excluded INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                embedding_type TEXT NOT NULL,
                faiss_id INTEGER,
                sparse_weights TEXT,
                model_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                duration_seconds INTEGER,
                metadata TEXT DEFAULT '{}'
            );

            -- Rich interaction log powering personalized ranking + the Now feed.
            -- Kept separate from `events` (which feeds the cognitive/activation
            -- subsystem with item-granular signals) so the two can evolve
            -- independently. Every row captures the (query -> result -> action)
            -- context at the moment of interaction, which is the feature space
            -- click-learning and recency-aware surfacing both need.
            CREATE TABLE IF NOT EXISTS interaction_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'local',
                event_type TEXT NOT NULL,           -- 'search' | 'click' | 'open' | 'dwell' | 'dismiss' | 'copy'
                item_id TEXT,                       -- NULL for bare 'search' events
                query TEXT,                         -- raw query string at the time of the action
                query_hash TEXT,                    -- stable hash of normalized query for join/aggregation
                rank_position INTEGER,              -- 1-indexed rank in the result list (for 'click')
                dwell_ms INTEGER,                   -- preview/view time in ms (for 'dwell')
                session_id TEXT,                    -- groups events in a contiguous user session
                client_timestamp TEXT,              -- client-side ISO time (for ordering across clock skew)
                metadata_json TEXT DEFAULT '{}',    -- free-form structured context
                created_at TEXT NOT NULL            -- server-side ISO insert time
            );

            CREATE TABLE IF NOT EXISTS relationships (
                id TEXT PRIMARY KEY,
                from_item_id TEXT NOT NULL,
                to_item_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Local forensic case registry and immutable evidence provenance.
            -- These tables are additive so existing local libraries migrate on
            -- the next init_db() without rewriting source evidence or items.
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS case_evidence (
                case_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                acquisition_status TEXT NOT NULL,
                UNIQUE(case_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS custody_events (
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                item_id TEXT,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watched_folders (
                path TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                added_at TEXT NOT NULL,
                is_default INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS watched_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                item_id TEXT REFERENCES items(id),
                last_seen REAL,
                file_size INTEGER,
                status TEXT DEFAULT 'indexed',
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            """
        )

        _ensure_items_columns(conn)
        _ensure_watched_files_columns(conn)
        _ensure_items_fts_schema(conn)
        _ensure_embeddings_schema(conn)
        _ensure_indexes(conn)
        fix_existing_timestamps(conn)
        backfill_watched_files(conn)
        retire_missing_local_items(conn)
        conn.commit()
    finally:
        conn.close()


def insert_item(conn: sqlite3.Connection, item: dict[str, Any]) -> str:
    payload = dict(item)
    _validate_columns(payload, ITEM_COLUMNS)

    item_id = str(payload.get("id") or uuid.uuid4())
    payload["id"] = item_id
    payload.setdefault("first_seen_at", _utc_now_iso())

    columns = list(payload.keys())
    placeholders = ", ".join("?" for _ in columns)
    values = [_serialize_value(payload[column]) for column in columns]

    started_at = time.perf_counter()
    conn.execute(
        f"""
        INSERT OR IGNORE INTO items ({", ".join(columns)})
        VALUES ({placeholders})
        """,
        values,
    )
    conn.commit()
    _print_db_write(
        "insert_item",
        str(payload.get("filename") or payload.get("id") or "unknown"),
        time.perf_counter() - started_at,
    )

    existing_item = get_item_by_hash(conn, str(payload["content_hash"]))
    if existing_item is not None:
        return str(existing_item["id"])
    return item_id


def get_item_by_hash(
    conn: sqlite3.Connection, content_hash: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM items WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    return _row_to_dict(row)


def get_item_by_id(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    return _row_to_dict(row)


def get_pending_ocr_items(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT id, original_path
        FROM items
        WHERE COALESCE(ocr_pending, 0) = 1
          AND COALESCE(is_excluded, 0) = 0
        ORDER BY first_seen_at ASC
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_item_by_path(
    conn: sqlite3.Connection,
    original_path: str | Path,
) -> dict[str, Any] | None:
    lookup_values = _path_lookup_values(original_path)
    placeholders = ", ".join("?" for _ in lookup_values)
    row = conn.execute(
        f"""
        SELECT *
        FROM items
        WHERE original_path IN ({placeholders})
          AND COALESCE(is_excluded, 0) = 0
        """,
        lookup_values,
    ).fetchone()
    return _row_to_dict(row)


def file_stat_exists(
    conn: sqlite3.Connection,
    original_path: str | Path,
    file_mtime: float,
    file_size_bytes: int,
) -> bool:
    lookup_values = _path_lookup_values(original_path)
    placeholders = ", ".join("?" for _ in lookup_values)
    row = conn.execute(
        f"""
        SELECT 1
        FROM watched_files
        WHERE path IN ({placeholders})
          AND last_seen = ?
          AND file_size = ?
          AND status IN ('indexed', 'duplicate')
        LIMIT 1
        """,
        (*lookup_values, float(file_mtime), int(file_size_bytes)),
    ).fetchone()
    return row is not None


def get_watched_file(
    conn: sqlite3.Connection,
    path: str | Path,
) -> dict[str, Any] | None:
    lookup_values = _path_lookup_values(path)
    placeholders = ", ".join("?" for _ in lookup_values)
    row = conn.execute(
        f"""
        SELECT *
        FROM watched_files
        WHERE path IN ({placeholders})
        ORDER BY CASE status
            WHEN 'indexed' THEN 0
            WHEN 'duplicate' THEN 1
            WHEN 'failed' THEN 2
            WHEN 'missing' THEN 3
            WHEN 'skipped_age' THEN 4
            WHEN 'pending' THEN 5
            WHEN 'error' THEN 6
            ELSE 7
        END,
        CASE WHEN item_id IS NULL THEN 1 ELSE 0 END
        LIMIT 1
        """,
        lookup_values,
    ).fetchone()
    return _row_to_dict(row)


def get_tracked_file_stats(conn: sqlite3.Connection) -> dict[str, tuple[float | None, int | None]]:
    rows = conn.execute(
        """
        SELECT path, last_seen, file_size
        FROM watched_files
        WHERE path IS NOT NULL
          AND status IN ('indexed', 'duplicate')
        """
    ).fetchall()
    return {
        str(row["path"]): (
            float(row["last_seen"]) if row["last_seen"] is not None else None,
            int(row["file_size"]) if row["file_size"] is not None else None,
        )
        for row in rows
        if row["path"]
    }


def update_item(conn: sqlite3.Connection, item_id: str, fields: dict[str, Any]) -> None:
    if not fields:
        return

    payload = dict(fields)
    if "id" in payload:
        raise ValueError("Updating item id is not supported")
    _validate_columns(payload, ITEM_COLUMNS - {"id"})

    assignments = ", ".join(f"{column} = ?" for column in payload)
    values = [_serialize_value(payload[column]) for column in payload]
    values.append(item_id)

    started_at = time.perf_counter()
    conn.execute(
        f"UPDATE items SET {assignments} WHERE id = ?",
        values,
    )
    conn.commit()
    _print_db_write("update_item", item_id, time.perf_counter() - started_at)


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 40,
    *,
    case_id: str | None = None,
) -> list[dict[str, Any]]:
    if not query.strip():
        return []

    case_clause = ""
    params: list[Any] = [query]
    if case_id is not None:
        case_clause = """
          AND EXISTS (
              SELECT 1 FROM case_evidence
              WHERE case_evidence.case_id = ?
                AND case_evidence.item_id = items.id
          )
        """
        params.append(str(case_id))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT items.*, bm25(items_fts) AS bm25_score
        FROM items
        JOIN items_fts ON items.rowid = items_fts.rowid
        WHERE items_fts MATCH ?
        {case_clause}
        ORDER BY bm25_score
        LIMIT ?
        """,
        params,
    ).fetchall()

    results = [dict(row) for row in rows]
    if not results:
        return results

    raw_scores = [float(result["bm25_score"]) for result in results]
    min_score = min(raw_scores)
    max_score = max(raw_scores)

    for result in results:
        raw_score = float(result["bm25_score"])
        if max_score == min_score:
            normalized = 1.0
        else:
            normalized = 1.0 - ((raw_score - min_score) / (max_score - min_score))
        result["bm25_score"] = max(0.0, min(1.0, normalized))

    return results


def get_recent_items(
    conn: sqlite3.Connection,
    days: int = 14,
    limit: int = 30,
    *,
    case_id: str | None = None,
) -> list[dict[str, Any]]:
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    case_clause = ""
    params: list[Any] = [threshold, threshold]
    if case_id is not None:
        case_clause = """
          AND EXISTS (
              SELECT 1 FROM case_evidence
              WHERE case_evidence.case_id = ?
                AND case_evidence.item_id = items.id
          )
        """
        params.append(str(case_id))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM items
        WHERE (
            (last_opened_at IS NOT NULL AND last_opened_at >= ?)
            OR first_seen_at >= ?
        )
        {case_clause}
        ORDER BY MAX(COALESCE(last_opened_at, ''), COALESCE(first_seen_at, '')) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def log_event(
    conn: sqlite3.Connection,
    item_id: str,
    event_type: str,
    duration_seconds: int | None = None,
) -> None:
    occurred_at = _utc_now_iso()
    event_id = str(uuid.uuid4())

    conn.execute(
        """
        INSERT INTO events (id, item_id, event_type, occurred_at, duration_seconds)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, item_id, event_type, occurred_at, duration_seconds),
    )

    if event_type == "open":
        conn.execute(
            """
            UPDATE items
            SET open_count = open_count + 1,
                last_opened_at = ?
            WHERE id = ?
            """,
            (occurred_at, item_id),
        )

    if event_type == "close" and duration_seconds is not None:
        conn.execute(
            """
            UPDATE items
            SET total_dwell_seconds = total_dwell_seconds + ?
            WHERE id = ?
            """,
            (duration_seconds, item_id),
        )

    conn.commit()


def get_item_count(conn: sqlite3.Connection) -> int:
    return get_count_summary(conn)["unique_memories"]


def get_count_summary(conn: sqlite3.Connection) -> dict[str, int]:
    watched_row = conn.execute(
        """
        SELECT
            COUNT(*) AS files_watched,
            COUNT(CASE WHEN status = 'duplicate' THEN 1 END) AS duplicates_skipped
        FROM watched_files
        """
    ).fetchone()
    items_row = conn.execute(
        """
        SELECT COUNT(*) AS unique_memories
        FROM items
        WHERE COALESCE(is_excluded, 0) = 0
        """
    ).fetchone()

    files_watched = int(watched_row["files_watched"]) if watched_row is not None and watched_row["files_watched"] is not None else 0
    duplicates_skipped = int(watched_row["duplicates_skipped"]) if watched_row is not None and watched_row["duplicates_skipped"] is not None else 0
    unique_memories = int(items_row["unique_memories"]) if items_row is not None and items_row["unique_memories"] is not None else 0

    return {
        "files_watched": files_watched,
        "unique_memories": unique_memories,
        "duplicates_skipped": duplicates_skipped,
    }


def get_pending_watched_file_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS pending_count
        FROM watched_files
        WHERE status = 'pending'
        """
    ).fetchone()
    return int(row["pending_count"]) if row and row["pending_count"] is not None else 0


def get_retryable_pending_files(
    conn: sqlite3.Connection,
    *,
    retry_cutoff: float | None = None,
    immediate_small_file_bytes: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    where_clauses = [
        "status = 'pending'",
        "(error_count IS NULL OR error_count < ?)",
    ]
    params: list[Any] = [MAX_WATCHED_FILE_ERRORS]

    retry_conditions: list[str] = []
    if retry_cutoff is not None:
        retry_conditions.append("(last_attempted IS NULL OR last_attempted < ?)")
        params.append(float(retry_cutoff))

    if immediate_small_file_bytes is not None:
        retry_conditions.append("(file_size IS NOT NULL AND file_size < ?)")
        params.append(int(immediate_small_file_bytes))

    if retry_conditions:
        where_clauses.append(f"({' OR '.join(retry_conditions)})")

    query = f"""
        SELECT path, file_size, last_attempted, error_count, last_error
        FROM watched_files
        WHERE {' AND '.join(where_clauses)}
        ORDER BY
            CASE
                WHEN file_size IS NULL THEN 1
                ELSE 0
            END,
            COALESCE(file_size, 0) ASC,
            COALESCE(last_attempted, 0) ASC,
            path ASC
    """

    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_faiss_id_to_item_id(
    conn: sqlite3.Connection, embedding_type: str
) -> dict[int, str]:
    rows = conn.execute(
        """
        SELECT faiss_id, item_id
        FROM embeddings
        WHERE embedding_type = ?
          AND faiss_id IS NOT NULL
        """,
        (embedding_type,),
    ).fetchall()
    return {int(row["faiss_id"]): str(row["item_id"]) for row in rows}


def get_watched_folders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT path, label, added_at, is_default
        FROM watched_folders
        ORDER BY is_default DESC, added_at, label
        """
    ).fetchall()
    return [dict(row) for row in rows]


def add_watched_folder(
    conn: sqlite3.Connection,
    path: str | Path,
    label: str,
    *,
    is_default: bool = False,
) -> None:
    path_str = _normalize_path(path)
    conn.execute(
        """
        INSERT OR IGNORE INTO watched_folders (path, label, added_at, is_default)
        VALUES (?, ?, ?, ?)
        """,
        (path_str, label, _utc_now_iso(), int(is_default)),
    )
    conn.commit()


def upsert_watched_file(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    item_id: str | None = None,
    last_seen: float | None = None,
    file_size: int | None = None,
    status: str = "pending",
    last_attempted: float | None = None,
) -> None:
    path_str = _normalize_path(path)
    started_at = time.perf_counter()
    conn.execute(
        """
        INSERT INTO watched_files (
            path,
            item_id,
            last_seen,
            file_size,
            status,
            added_at,
            error_count,
            last_error,
            last_attempted
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)
        ON CONFLICT(path) DO UPDATE SET
            item_id = COALESCE(excluded.item_id, watched_files.item_id),
            last_seen = COALESCE(excluded.last_seen, watched_files.last_seen),
            file_size = COALESCE(excluded.file_size, watched_files.file_size),
            status = excluded.status,
            error_count = CASE
                WHEN excluded.status IN ('indexed', 'duplicate')
                THEN 0
                ELSE watched_files.error_count
            END,
            last_error = CASE
                WHEN excluded.status IN ('indexed', 'duplicate')
                THEN NULL
                ELSE watched_files.last_error
            END,
            last_attempted = COALESCE(excluded.last_attempted, watched_files.last_attempted)
        """,
        (
            path_str,
            item_id,
            last_seen,
            file_size,
            status,
            _utc_now_iso(),
            last_attempted,
        ),
    )
    conn.commit()
    _print_db_write("watched_file", path_str, time.perf_counter() - started_at)


def record_watched_file_attempt(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    last_seen: float | None = None,
    file_size: int | None = None,
    attempted_at: float | None = None,
) -> float:
    path_str = _normalize_path(path)
    attempted = float(attempted_at if attempted_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO watched_files (
            path,
            item_id,
            last_seen,
            file_size,
            status,
            added_at,
            error_count,
            last_error,
            last_attempted
        )
        VALUES (?, NULL, ?, ?, 'pending', ?, 0, NULL, ?)
        ON CONFLICT(path) DO UPDATE SET
            last_seen = COALESCE(excluded.last_seen, watched_files.last_seen),
            file_size = COALESCE(excluded.file_size, watched_files.file_size),
            status = 'pending',
            last_attempted = excluded.last_attempted
        """,
        (path_str, last_seen, file_size, _utc_now_iso(), attempted),
    )
    conn.commit()
    return attempted


def record_watched_file_failure(
    conn: sqlite3.Connection,
    path: str | Path,
    error_message: str,
    *,
    item_id: str | None = None,
    last_seen: float | None = None,
    file_size: int | None = None,
    attempted_at: float | None = None,
) -> tuple[int, str]:
    path_str = _normalize_path(path)
    attempted = float(attempted_at if attempted_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO watched_files (
            path,
            item_id,
            last_seen,
            file_size,
            status,
            added_at,
            error_count,
            last_error,
            last_attempted
        )
        VALUES (?, ?, ?, ?, 'pending', ?, 0, NULL, ?)
        ON CONFLICT(path) DO UPDATE SET
            item_id = COALESCE(excluded.item_id, watched_files.item_id),
            last_seen = COALESCE(excluded.last_seen, watched_files.last_seen),
            file_size = COALESCE(excluded.file_size, watched_files.file_size),
            last_attempted = excluded.last_attempted
        """,
        (
            path_str,
            item_id,
            last_seen,
            file_size,
            _utc_now_iso(),
            attempted,
        ),
    )
    conn.execute(
        """
        UPDATE watched_files
        SET error_count = COALESCE(error_count, 0) + 1,
            last_error = ?,
            last_attempted = ?,
            status = CASE
                WHEN COALESCE(error_count, 0) + 1 >= ?
                THEN 'failed'
                ELSE 'pending'
            END
        WHERE path = ?
        """,
        (
            str(error_message or "")[:200],
            attempted,
            MAX_WATCHED_FILE_ERRORS,
            path_str,
        ),
    )
    row = conn.execute(
        "SELECT error_count, status FROM watched_files WHERE path = ?",
        (path_str,),
    ).fetchone()
    conn.commit()
    if row is None:
        return 1, "pending"
    return int(row["error_count"] or 0), str(row["status"] or "pending")


def delete_watched_file(conn: sqlite3.Connection, path: str | Path) -> None:
    lookup_values = _path_lookup_values(path)
    placeholders = ", ".join("?" for _ in lookup_values)
    _retire_items_for_paths(conn, [path])
    conn.execute(
        f"DELETE FROM watched_files WHERE path IN ({placeholders})",
        lookup_values,
    )
    conn.commit()


def mark_watched_files_pending(
    conn: sqlite3.Connection,
    paths: list[str | Path],
) -> None:
    if not paths:
        return

    rows = [(_normalize_path(path),) for path in paths]
    conn.executemany(
        """
        UPDATE watched_files
        SET status = 'pending'
        WHERE path = ?
        """,
        rows,
    )
    conn.commit()


def mark_watched_file_dataless(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    item_id: str | None = None,
    last_seen: float | None = None,
    file_size: int | None = None,
    attempted_at: float | None = None,
) -> None:
    path_str = _normalize_path(path)
    conn.execute(
        """
        INSERT INTO watched_files (
            path,
            item_id,
            last_seen,
            file_size,
            status,
            added_at,
            error_count,
            last_error,
            last_attempted
        )
        VALUES (?, ?, ?, ?, 'error', ?, 0, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            item_id = COALESCE(excluded.item_id, watched_files.item_id),
            last_seen = COALESCE(excluded.last_seen, watched_files.last_seen),
            file_size = COALESCE(excluded.file_size, watched_files.file_size),
            status = 'error',
            error_count = 0,
            last_error = excluded.last_error,
            last_attempted = COALESCE(excluded.last_attempted, watched_files.last_attempted)
        """,
        (
            path_str,
            item_id,
            last_seen,
            file_size,
            _utc_now_iso(),
            DATALLESS_FILE_ERROR,
            attempted_at,
        ),
    )
    conn.commit()


def set_watched_files_status(
    conn: sqlite3.Connection,
    paths: list[str | Path],
    status: str,
) -> None:
    if not paths:
        return

    rows = [(status, _normalize_path(path)) for path in paths]
    conn.executemany(
        """
        UPDATE watched_files
        SET status = ?
        WHERE path = ?
        """,
        rows,
    )
    conn.commit()


def sync_watched_files_for_scan(
    conn: sqlite3.Connection,
    scanned_roots: list[str | Path],
    all_files: list[str | Path],
) -> None:
    normalized_roots = [_normalize_path(root) for root in scanned_roots]
    seen_paths: set[str] = set()
    upserts: list[tuple[str, float | None, int | None, str]] = []

    for file_path in all_files:
        resolved = Path(file_path).expanduser().resolve()
        path_str = str(resolved)
        seen_paths.add(path_str)
        try:
            stat_result = resolved.stat()
            last_seen = float(stat_result.st_mtime)
            file_size = int(stat_result.st_size)
        except OSError:
            last_seen = None
            file_size = None
        upserts.append((path_str, last_seen, file_size, _utc_now_iso()))

    conn.executemany(
        """
        INSERT INTO watched_files (path, last_seen, file_size, status, added_at)
        VALUES (?, ?, ?, 'pending', ?)
        ON CONFLICT(path) DO UPDATE SET
            last_seen = excluded.last_seen,
            file_size = excluded.file_size
        """,
        upserts,
    )

    for root in normalized_roots:
        rows = conn.execute(
            """
            SELECT path
            FROM watched_files
            WHERE path = ?
               OR path LIKE ?
            """,
            (root, f"{root}/%"),
        ).fetchall()
        stale_paths = [
            (str(row["path"]),)
            for row in rows
            if str(row["path"]) not in seen_paths
        ]
        if stale_paths:
            _retire_items_for_paths(conn, [path for (path,) in stale_paths])
            conn.executemany("DELETE FROM watched_files WHERE path = ?", stale_paths)

    conn.commit()


def remove_watched_folder(conn: sqlite3.Connection, path: str | Path) -> None:
    path_str = _normalize_path(path)
    _retire_items_under_root(conn, path_str)
    conn.execute(
        """
        DELETE FROM watched_folders
        WHERE path = ?
          AND COALESCE(is_default, 0) = 0
        """,
        (path_str,),
    )
    conn.execute(
        """
        DELETE FROM watched_files
        WHERE path = ?
           OR path LIKE ?
        """,
        (path_str, f"{path_str}/%"),
    )
    conn.commit()


def create_case(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str | None = None,
    notes: str | None = None,
    status: str = "active",
    case_id: str | None = None,
) -> dict[str, Any]:
    now = _utc_now_iso()
    payload = {
        "id": str(case_id or uuid.uuid4()),
        "name": str(name),
        "description": description,
        "status": str(status),
        "created_at": now,
        "updated_at": now,
        "notes": notes,
    }
    conn.execute(
        """
        INSERT INTO cases (id, name, description, status, created_at, updated_at, notes)
        VALUES (:id, :name, :description, :status, :created_at, :updated_at, :notes)
        """,
        payload,
    )
    conn.commit()
    return payload


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    return _row_to_dict(
        conn.execute("SELECT * FROM cases WHERE id = ?", (str(case_id),)).fetchone()
    )


def list_cases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT cases.*, COUNT(case_evidence.item_id) AS evidence_count
        FROM cases
        LEFT JOIN case_evidence ON case_evidence.case_id = cases.id
        GROUP BY cases.id
        ORDER BY cases.updated_at DESC, cases.created_at DESC, cases.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def update_case(
    conn: sqlite3.Connection,
    case_id: str,
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    payload = dict(fields)
    payload.pop("updated_at", None)
    _validate_columns(payload, CASE_COLUMNS - {"id", "created_at", "updated_at"})
    if not payload:
        return get_case(conn, case_id)

    payload["updated_at"] = _utc_now_iso()
    assignments = ", ".join(f"{column} = ?" for column in payload)
    values = [_serialize_value(payload[column]) for column in payload]
    values.append(str(case_id))
    conn.execute(f"UPDATE cases SET {assignments} WHERE id = ?", values)
    conn.commit()
    return get_case(conn, case_id)


def delete_case(conn: sqlite3.Connection, case_id: str) -> None:
    conn.execute("DELETE FROM custody_events WHERE case_id = ?", (str(case_id),))
    conn.execute("DELETE FROM case_evidence WHERE case_id = ?", (str(case_id),))
    conn.execute("DELETE FROM cases WHERE id = ?", (str(case_id),))
    conn.commit()


def upsert_case_evidence(
    conn: sqlite3.Connection,
    evidence: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, Any]:
    payload = dict(evidence)
    _validate_columns(payload, CASE_EVIDENCE_COLUMNS)
    conn.execute(
        """
        INSERT INTO case_evidence (
            case_id, item_id, source_path, sha256, size_bytes, mtime_ns,
            imported_at, acquisition_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(case_id, item_id) DO UPDATE SET
            source_path = excluded.source_path,
            sha256 = excluded.sha256,
            size_bytes = excluded.size_bytes,
            mtime_ns = excluded.mtime_ns,
            imported_at = excluded.imported_at,
            acquisition_status = excluded.acquisition_status
        """,
        (
            payload["case_id"],
            payload["item_id"],
            payload["source_path"],
            payload["sha256"],
            int(payload["size_bytes"]),
            int(payload["mtime_ns"]),
            payload["imported_at"],
            payload["acquisition_status"],
        ),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        """
        SELECT * FROM case_evidence
        WHERE case_id = ? AND item_id = ?
        """,
        (payload["case_id"], payload["item_id"]),
    ).fetchone()
    return dict(row) if row is not None else payload


def list_case_evidence(
    conn: sqlite3.Connection,
    case_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            evidence.*,
            items.filename,
            items.title,
            items.file_type,
            items.handler_type,
            items.source_type,
            items.original_path,
            items.extracted_text,
            items.summary,
            items.preview_text,
            items.created_at AS item_created_at,
            items.modified_at AS item_modified_at,
            items.first_seen_at AS item_first_seen_at,
            items.file_mtime AS indexed_file_mtime,
            items.file_size_bytes AS indexed_file_size_bytes,
            items.is_excluded,
            watched.status AS indexed_status,
            watched.last_seen AS indexed_last_seen,
            watched.file_size AS indexed_watched_file_size
        FROM case_evidence AS evidence
        LEFT JOIN items ON items.id = evidence.item_id
        LEFT JOIN watched_files AS watched
          ON watched.path = evidence.source_path
        WHERE evidence.case_id = ?
        ORDER BY evidence.imported_at, evidence.source_path, evidence.item_id
        """,
        (str(case_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def get_case_item_ids(conn: sqlite3.Connection, case_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT item_id FROM case_evidence WHERE case_id = ?",
        (str(case_id),),
    ).fetchall()
    return {str(row["item_id"]) for row in rows if row["item_id"]}


def get_item_provenance(
    conn: sqlite3.Connection,
    item_ids: list[str] | set[str],
) -> dict[str, dict[str, Any]]:
    unique_ids = [str(item_id) for item_id in dict.fromkeys(item_ids) if item_id]
    if not unique_ids:
        return {}

    placeholders = ", ".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT
            item_id,
            case_id,
            sha256,
            size_bytes,
            mtime_ns,
            acquisition_status,
            imported_at
        FROM case_evidence
        WHERE item_id IN ({placeholders})
        ORDER BY item_id, imported_at DESC, case_id
        """,
        unique_ids,
    ).fetchall()

    provenance: dict[str, dict[str, Any]] = {}
    for row in rows:
        item_id = str(row["item_id"])
        current = provenance.setdefault(
            item_id,
            {
                "case_ids": [],
                "evidence_sha256": None,
                "evidence_size_bytes": None,
                "evidence_mtime_ns": None,
                "acquisition_status": None,
            },
        )
        case_id = str(row["case_id"])
        if case_id not in current["case_ids"]:
            current["case_ids"].append(case_id)
        if current["evidence_sha256"] is None:
            current.update(
                {
                    "evidence_sha256": row["sha256"],
                    "evidence_size_bytes": row["size_bytes"],
                    "evidence_mtime_ns": row["mtime_ns"],
                    "acquisition_status": row["acquisition_status"],
                }
            )
    return provenance


def update_case_evidence_status(
    conn: sqlite3.Connection,
    case_id: str,
    item_id: str,
    status: str,
    *,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        UPDATE case_evidence
        SET acquisition_status = ?
        WHERE case_id = ? AND item_id = ?
        """,
        (str(status), str(case_id), str(item_id)),
    )
    if commit:
        conn.commit()


def append_custody_event(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    item_id: str | None,
    event_type: str,
    actor: str,
    occurred_at: str | None = None,
    details: dict[str, Any] | None = None,
    event_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Append one event to a case-local SHA-256 hash chain."""
    if commit:
        conn.commit()
    previous_row = conn.execute(
        """
        SELECT event_hash
        FROM custody_events
        WHERE case_id = ?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (str(case_id),),
    ).fetchone()
    previous_hash = str(previous_row["event_hash"]) if previous_row else None
    details_json = canonical_json(details or {})
    payload = {
        "id": str(event_id or uuid.uuid4()),
        "case_id": str(case_id),
        "item_id": str(item_id) if item_id is not None else None,
        "event_type": str(event_type),
        "actor": str(actor),
        "occurred_at": str(occurred_at or _utc_now_iso()),
        "details_json": details_json,
        "previous_event_hash": previous_hash,
    }
    payload["event_hash"] = compute_custody_event_hash(payload)
    _validate_columns(payload, CUSTODY_EVENT_COLUMNS)
    conn.execute(
        """
        INSERT INTO custody_events (
            id, case_id, item_id, event_type, actor, occurred_at,
            details_json, previous_event_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["id"],
            payload["case_id"],
            payload["item_id"],
            payload["event_type"],
            payload["actor"],
            payload["occurred_at"],
            payload["details_json"],
            payload["previous_event_hash"],
            payload["event_hash"],
        ),
    )
    if commit:
        conn.commit()
    return payload


def record_case_evidence_acquisition(
    conn: sqlite3.Connection,
    evidence: dict[str, Any],
    *,
    actor: str,
    occurred_at: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Persist an evidence row and its acquisition event atomically."""
    already_in_transaction = conn.in_transaction
    if not already_in_transaction:
        conn.execute("BEGIN")

    try:
        upsert_case_evidence(conn, evidence, commit=False)
        event = append_custody_event(
            conn,
            case_id=str(evidence["case_id"]),
            item_id=str(evidence["item_id"]),
            event_type="evidence_acquired",
            actor=actor,
            occurred_at=occurred_at,
            details=details,
            commit=False,
        )
        if not already_in_transaction:
            conn.commit()
        return event
    except Exception:
        if not already_in_transaction:
            conn.rollback()
        raise


def record_case_evidence_verification(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    item_id: str,
    status: str,
    actor: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Persist the verification status and event as one durable operation."""
    already_in_transaction = conn.in_transaction
    if not already_in_transaction:
        conn.execute("BEGIN")

    try:
        update_case_evidence_status(
            conn,
            case_id,
            item_id,
            status,
            commit=False,
        )
        event = append_custody_event(
            conn,
            case_id=case_id,
            item_id=item_id,
            event_type="evidence_verified",
            actor=actor,
            details=details,
            commit=False,
        )
        if not already_in_transaction:
            conn.commit()
        return event
    except Exception:
        if not already_in_transaction:
            conn.rollback()
        raise


def list_custody_events(
    conn: sqlite3.Connection,
    case_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM custody_events
        WHERE case_id = ?
        ORDER BY occurred_at, rowid
        """,
        (str(case_id),),
    ).fetchall()
    return [dict(row) for row in rows]
