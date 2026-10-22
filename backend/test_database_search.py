from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import database


class DatabaseSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        self.previous_db_path = database.DB_PATH
        self.previous_config_db_path = database.config.DB_PATH
        database.DB_PATH = self.db_path
        database.config.DB_PATH = self.db_path
        database.init_db()

    def tearDown(self) -> None:
        database.DB_PATH = self.previous_db_path
        database.config.DB_PATH = self.previous_config_db_path
        self.temp_dir.cleanup()

    def test_fts_covers_title_preview_and_original_path(self) -> None:
        conn = database.get_connection()
        try:
            database.insert_item(
                conn,
                {
                    "id": "lexical-fields-1",
                    "content_hash": "lexical-fields-hash-1",
                    "title": "Apollo Architecture Brief",
                    "filename": "unrelated-note.txt",
                    "file_type": "text",
                    "handler_type": "text",
                    "source_type": "local",
                    "original_path": "/evidence/orion/unique-path-marker/unrelated-note.txt",
                    "first_seen_at": "2026-10-20T12:00:00+00:00",
                    "preview_text": "Quartz preview marker",
                    "extracted_text": "",
                    "summary": "",
                    "keywords": "[]",
                },
            )

            for query in ("apollo", "quartz", "orion"):
                rows = database.search_fts(conn, query)
                self.assertIn("lexical-fields-1", {row["id"] for row in rows})
        finally:
            conn.close()

    def test_legacy_fts_schema_is_migrated_without_changing_items_schema(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        previous_db_path = database.DB_PATH
        previous_config_db_path = database.config.DB_PATH
        database.DB_PATH = legacy_path
        database.config.DB_PATH = legacy_path
        try:
            conn = sqlite3.connect(legacy_path)
            conn.executescript(
                """
                CREATE TABLE items (
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
                CREATE VIRTUAL TABLE items_fts
                USING fts5(
                    filename,
                    extracted_text,
                    summary,
                    keywords,
                    content='items',
                    content_rowid='rowid',
                    tokenize='porter unicode61'
                );
                INSERT INTO items (
                    id, content_hash, title, filename, file_type, handler_type,
                    original_path, first_seen_at, preview_text
                )
                VALUES (
                    'legacy-item-1', 'legacy-hash-1', 'Legacy title',
                    'legacy-file.txt', 'text', 'text',
                    '/evidence/legacy/legacy-file.txt',
                    '2026-10-20T12:00:00+00:00', 'Legacy preview marker'
                );
                INSERT INTO items_fts(rowid, filename, extracted_text, summary, keywords)
                SELECT rowid, filename, extracted_text, summary, keywords
                FROM items;
                """
            )
            conn.commit()
            conn.close()

            database.init_db()

            conn = database.get_connection()
            try:
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(items)").fetchall()
                }
                fts_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(items_fts)").fetchall()
                }
                rows = database.search_fts(conn, "marker")
            finally:
                conn.close()

            self.assertIn("preview_text", columns)
            self.assertTrue(
                {
                    "title",
                    "filename",
                    "preview_text",
                    "original_path",
                    "extracted_text",
                    "summary",
                    "keywords",
                } <= fts_columns
            )
            self.assertEqual([row["id"] for row in rows], ["legacy-item-1"])
        finally:
            database.DB_PATH = previous_db_path
            database.config.DB_PATH = previous_config_db_path

    def test_list_cases_includes_evidence_count(self) -> None:
        conn = database.get_connection()
        try:
            database.create_case(conn, name="Empty case", case_id="case-empty")
            database.create_case(conn, name="Populated case", case_id="case-full")
            conn.execute(
                """
                INSERT INTO case_evidence (
                    case_id, item_id, source_path, sha256, size_bytes,
                    mtime_ns, imported_at, acquisition_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "case-full",
                    "item-1",
                    "/evidence/item.txt",
                    "hash-1",
                    4,
                    1,
                    "2026-10-20T12:00:00+00:00",
                    "acquired",
                ),
            )
            conn.commit()

            cases = {case["id"]: case for case in database.list_cases(conn)}
        finally:
            conn.close()

        self.assertEqual(cases["case-empty"]["evidence_count"], 0)
        self.assertEqual(cases["case-full"]["evidence_count"], 1)


if __name__ == "__main__":
    unittest.main()
