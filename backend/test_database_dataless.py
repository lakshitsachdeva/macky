import sqlite3
import unittest

from backend import database


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE watched_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            item_id TEXT,
            last_seen REAL,
            file_size INTEGER,
            status TEXT DEFAULT 'indexed',
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            error_count INTEGER DEFAULT 0,
            last_error TEXT,
            last_attempted REAL
        );
        """
    )
    return conn


class MarkWatchedFileDatalessTests(unittest.TestCase):
    def test_insert_sets_clear_dataless_error(self) -> None:
        conn = _make_conn()
        self.addCleanup(conn.close)
        path = database._normalize_path("/tmp/cloud-only.png")

        database.mark_watched_file_dataless(
            conn,
            path,
            last_seen=123.0,
            file_size=456,
            attempted_at=789.0,
        )

        row = conn.execute(
            "SELECT status, error_count, last_error, last_attempted FROM watched_files WHERE path = ?",
            (path,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_count"], 0)
        self.assertEqual(row["last_error"], database.DATALLESS_FILE_ERROR)
        self.assertEqual(row["last_attempted"], 789.0)

    def test_existing_timeout_row_is_rewritten(self) -> None:
        conn = _make_conn()
        self.addCleanup(conn.close)
        path = database._normalize_path("/tmp/already-timed-out.png")

        conn.execute(
            """
            INSERT INTO watched_files (
                path, item_id, last_seen, file_size, status, added_at, error_count, last_error, last_attempted
            )
            VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP, 2, ?, ?)
            """,
            (
                path,
                "item-123",
                10.0,
                20,
                "TimeoutError: [Errno 60] Operation timed out",
                30.0,
            ),
        )
        conn.commit()

        database.mark_watched_file_dataless(
            conn,
            path,
            item_id="item-123",
            last_seen=11.0,
            file_size=21,
            attempted_at=31.0,
        )

        row = conn.execute(
            """
            SELECT item_id, last_seen, file_size, status, error_count, last_error, last_attempted
            FROM watched_files
            WHERE path = ?
            """,
            (path,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["item_id"], "item-123")
        self.assertEqual(row["last_seen"], 11.0)
        self.assertEqual(row["file_size"], 21)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_count"], 0)
        self.assertEqual(row["last_error"], database.DATALLESS_FILE_ERROR)
        self.assertEqual(row["last_attempted"], 31.0)


if __name__ == "__main__":
    unittest.main()
