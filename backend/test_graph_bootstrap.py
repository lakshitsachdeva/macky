from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.graph.bootstrap import bootstrap_memory_db
from backend.graph.store import GraphStore


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
            doc_type_guess TEXT,
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
            doc_type_guess, first_seen_at, created_at, modified_at, last_opened_at,
            open_count, total_dwell_seconds, copy_event_count, is_excluded
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        [
            (
                "email-1",
                "follow-up.eml",
                "Term Sheet Follow Up",
                "text",
                "text",
                "gmail",
                "Rahul Sharma",
                "rahul@acme.com",
                "Follow up on the term sheet",
                "Subject: Term Sheet Follow Up\nTo: Lakshit\nCan you review the term sheet this week?",
                None,
                '["fundraising","term sheet"]',
                "email",
                "2026-04-01T09:00:00+00:00",
                "2026-04-01T09:00:00+00:00",
                "2026-04-01T09:00:00+00:00",
                "2026-04-02T10:00:00+00:00",
                2,
                300,
                0,
            ),
            (
                "proposal-1",
                "website-revamp.pdf",
                "Website Revamp Proposal",
                "pdf",
                "text",
                "local",
                None,
                None,
                "Proposal for Acme Corp website revamp",
                "proposal client: Acme Corp deliverables redesign and analytics submitted deadline April 20, 2026",
                None,
                '["proposal","website revamp"]',
                "proposal",
                "2026-04-03T10:00:00+00:00",
                "2026-04-03T10:00:00+00:00",
                "2026-04-03T10:00:00+00:00",
                None,
                1,
                0,
                0,
            ),
            (
                "deck-1",
                "recurdo-deck.pptx",
                "Recurdo Seed Deck",
                "presentation",
                "text",
                "local",
                None,
                None,
                "Investor deck for Recurdo",
                "pitch deck investor deck seed round market opportunity team Lakshit Rahul",
                None,
                '["fundraising","investor"]',
                "presentation",
                "2026-04-04T10:00:00+00:00",
                "2026-04-04T10:00:00+00:00",
                "2026-04-04T10:00:00+00:00",
                "2026-04-05T10:00:00+00:00",
                3,
                500,
                1,
            ),
        ],
    )
    conn.commit()
    conn.close()


class GraphBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.memory_db = Path(self.temp_dir.name) / "memory.db"
        self.graph_db = Path(self.temp_dir.name) / "graph.db"
        _create_memory_db(self.memory_db)

    def test_bootstrap_populates_graph_store(self) -> None:
        summary = bootstrap_memory_db(
            self.memory_db,
            graph_db_path=self.graph_db,
            reset=True,
        )
        store = GraphStore(self.graph_db)
        counts = store.get_counts()

        self.assertEqual(summary["items"], 3)
        self.assertGreater(summary["entities"], 4)
        self.assertGreater(summary["episode_mentions"], 4)
        self.assertGreater(summary["temporal_facts"], 0)
        self.assertEqual(counts["temporal_facts"], summary["temporal_facts"])

        proposal_timeline = store.get_entity_timeline("Website Revamp Proposal")
        self.assertTrue(any(entry["predicate"] == "HAS_STATUS" for entry in proposal_timeline))

        rahul = store.get_entity_by_name("Rahul Sharma")
        self.assertIsNotNone(rahul)
        self.assertGreaterEqual(int(rahul["mention_count"]), 1)

    def test_dry_run_does_not_create_graph_db(self) -> None:
        summary = bootstrap_memory_db(
            self.memory_db,
            graph_db_path=self.graph_db,
            dry_run=True,
            reset=True,
        )

        self.assertEqual(summary["items"], 3)
        self.assertFalse(self.graph_db.exists())


if __name__ == "__main__":
    unittest.main()
