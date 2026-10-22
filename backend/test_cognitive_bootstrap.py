from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.cognition.bootstrap import bootstrap_memory_db
from backend.cognition.store import CognitiveStore


def _create_memory_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            filename TEXT,
            title TEXT,
            file_type TEXT,
            handler_type TEXT,
            source_type TEXT,
            sender_name TEXT,
            sender_email TEXT,
            preview_text TEXT,
            extracted_text TEXT,
            summary TEXT,
            keywords TEXT,
            first_seen_at TEXT,
            created_at TEXT,
            modified_at TEXT,
            last_opened_at TEXT,
            open_count INTEGER DEFAULT 0,
            total_dwell_seconds INTEGER DEFAULT 0,
            copy_event_count INTEGER DEFAULT 0,
            is_excluded INTEGER DEFAULT 0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO items (
            id, filename, title, file_type, handler_type, source_type,
            sender_name, sender_email, preview_text, extracted_text, summary, keywords,
            first_seen_at, created_at, modified_at, last_opened_at, open_count,
            total_dwell_seconds, copy_event_count, is_excluded
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        [
            (
                "item-1",
                "deck.pptx",
                "Recurdo Seed Deck",
                "presentation",
                "text",
                "local",
                None,
                None,
                "investor deck for recurdo",
                "pitch deck investor seed round market opportunity",
                None,
                '["fundraising","investor"]',
                "2026-04-01T10:00:00+00:00",
                "2026-04-01T10:00:00+00:00",
                "2026-04-02T11:00:00+00:00",
                "2026-04-03T09:00:00+00:00",
                3,
                600,
                1,
            ),
            (
                "item-2",
                "notes.txt",
                "Team Meeting Notes",
                "text",
                "text",
                "local",
                None,
                None,
                "weekly sync notes",
                "meeting minutes roadmap sprint blockers",
                None,
                '["meeting","notes"]',
                "2026-04-02T10:00:00+00:00",
                "2026-04-02T10:00:00+00:00",
                "2026-04-02T10:00:00+00:00",
                None,
                0,
                0,
                0,
            ),
        ],
    )
    conn.commit()
    conn.close()


class CognitiveBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.memory_db = Path(self.temp_dir.name) / "memory.db"
        self.cognitive_db = Path(self.temp_dir.name) / "cognitive_layer.db"
        _create_memory_db(self.memory_db)

    def test_bootstrap_populates_cognitive_store(self) -> None:
        summary = bootstrap_memory_db(
            self.memory_db,
            cognitive_db_path=self.cognitive_db,
            reset=True,
        )
        store = CognitiveStore(self.cognitive_db)

        self.assertEqual(summary["items"], 2)
        self.assertGreater(summary["activation_events"], 0)
        self.assertGreater(summary["entity_edges"], 0)
        self.assertEqual(summary["frame_instances"], 2)
        self.assertIsNotNone(store.get_frame_instance("item-1"))
        self.assertGreater(len(store.get_activation_history("item-1")), 1)

    def test_bootstrap_is_idempotent(self) -> None:
        bootstrap_memory_db(self.memory_db, cognitive_db_path=self.cognitive_db, reset=True)
        bootstrap_memory_db(self.memory_db, cognitive_db_path=self.cognitive_db, reset=False)

        store = CognitiveStore(self.cognitive_db)
        counts = store.get_counts()
        self.assertEqual(counts["frame_instances"], 2)
        self.assertGreater(counts["entity_edges"], 0)

    def test_dry_run_does_not_create_cognitive_db(self) -> None:
        summary = bootstrap_memory_db(
            self.memory_db,
            cognitive_db_path=self.cognitive_db,
            dry_run=True,
            reset=True,
        )

        self.assertEqual(summary["items"], 2)
        self.assertFalse(self.cognitive_db.exists())

    def test_bootstrap_emits_progress_events(self) -> None:
        progress_events: list[dict[str, object]] = []

        summary = bootstrap_memory_db(
            self.memory_db,
            cognitive_db_path=self.cognitive_db,
            reset=True,
            progress_callback=progress_events.append,
        )

        self.assertGreaterEqual(len(progress_events), 3)
        self.assertEqual(progress_events[0]["state"], "running")
        self.assertEqual(progress_events[0]["phase"], "loading")
        self.assertIn("processing", {event["phase"] for event in progress_events})
        self.assertIn("writing", {event["phase"] for event in progress_events})
        self.assertEqual(progress_events[-1]["state"], "completed")
        self.assertEqual(progress_events[-1]["summary"]["items"], summary["items"])


if __name__ == "__main__":
    unittest.main()
