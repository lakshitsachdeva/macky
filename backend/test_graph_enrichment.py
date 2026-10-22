from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.graph.enrichment import (
    backfill_document_co_occurrences,
    backfill_email_facts,
    extract_document_co_occurrences,
)
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
            original_path TEXT,
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
            id, filename, title, file_type, handler_type, source_type, original_path,
            sender_name, sender_email, preview_text, extracted_text, summary, keywords,
            doc_type_guess, first_seen_at, created_at, modified_at, last_opened_at,
            open_count, total_dwell_seconds, copy_event_count, is_excluded
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        [
            (
                "gmail-1",
                "term-sheet-follow-up.txt",
                "Term Sheet Follow Up",
                "text",
                "text",
                "gmail",
                "https://mail.google.com/mail/u/0/#all/msg-1",
                "Rahul Sharma",
                "rahul@acme.com",
                "Follow up email",
                "Subject: Term Sheet Follow Up\nTo: Lakshit <lakshit@example.com>\nCan you review the term sheet?",
                "Follow up email",
                '["email","thread:thread-1"]',
                "email",
                "2026-04-01T10:00:00+00:00",
                "2026-04-01T10:00:00+00:00",
                "2026-04-01T10:00:00+00:00",
                None,
                0,
                0,
                0,
            ),
            (
                "gmail-2",
                "re-term-sheet-follow-up.txt",
                "Re: Term Sheet Follow Up",
                "text",
                "text",
                "gmail",
                "https://mail.google.com/mail/u/0/#all/msg-2",
                "Rahul Sharma",
                "rahul@acme.com",
                "Reply email",
                "Subject: Re: Term Sheet Follow Up\nTo: Lakshit <lakshit@example.com>\nThanks, following up again.",
                "Reply email",
                '["email","thread:thread-1"]',
                "email",
                "2026-04-02T10:00:00+00:00",
                "2026-04-02T10:00:00+00:00",
                "2026-04-02T10:00:00+00:00",
                None,
                0,
                0,
                0,
            ),
            (
                "proposal-1",
                "proposal.pdf",
                "Recurdo Research Proposal",
                "pdf",
                "text",
                "local",
                "/tmp/proposal.pdf",
                None,
                None,
                "Proposal draft",
                (
                    "Research proposal by Lakshit Sachdeva and Rahul Sharma for Recurdo. "
                    "Lakshit Sachdeva wrote the technical plan. Rahul Sharma wrote the market plan. "
                    "Lakshit Sachdeva and Rahul Sharma revised the proposal together. "
                    "See investor_update.pdf and the Recurdo proposal for follow-ups. "
                    "This document is intentionally long enough to exceed two hundred characters."
                ),
                "Proposal draft",
                '["proposal","recurdo"]',
                "proposal",
                "2026-04-03T10:00:00+00:00",
                "2026-04-03T10:00:00+00:00",
                "2026-04-03T10:00:00+00:00",
                None,
                0,
                0,
                0,
            ),
        ],
    )
    conn.commit()
    conn.close()


class GraphEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.memory_db = base / "memory.db"
        self.graph_store = GraphStore(base / "graph.db")
        _create_memory_db(self.memory_db)

    def test_backfill_email_facts_creates_structured_relationships(self) -> None:
        self.graph_store.upsert_entity(name="Acme", entity_type="Organisation")
        summary = backfill_email_facts(
            memory_db_path=self.memory_db,
            graph_store=self.graph_store,
        )

        self.assertEqual(summary["processed"], 2)
        self.assertGreaterEqual(summary["sent_to_created"], 2)
        self.assertGreaterEqual(summary["weak_signals"], 2)
        self.assertGreaterEqual(summary["responds_to_created"], 1)

        timeline = self.graph_store.get_entity_timeline("Rahul Sharma")
        self.assertTrue(
            any(
                entry["predicate"] == "SENT_TO"
                and {entry["subject"], entry["object"]} == {"Rahul Sharma", "lakshit@example.com"}
                for entry in timeline
            )
        )
        self.assertTrue(
            any(
                entry["predicate"] == "REPRESENTS_ORG"
                and {entry["subject"], entry["object"]} == {"Rahul Sharma", "Acme"}
                for entry in timeline
            )
        )

    def test_document_co_occurrence_creates_weak_signals_and_references(self) -> None:
        conn = sqlite3.connect(self.memory_db)
        conn.row_factory = sqlite3.Row
        try:
            item = dict(conn.execute("SELECT * FROM items WHERE id = 'proposal-1'").fetchone())
        finally:
            conn.close()

        result = extract_document_co_occurrences(item, self.graph_store)

        self.assertGreaterEqual(result["weak_signals"], 1)
        self.assertGreaterEqual(result["references"], 1)

    def test_backfill_document_co_occurrences_processes_local_docs(self) -> None:
        summary = backfill_document_co_occurrences(
            memory_db_path=self.memory_db,
            graph_store=self.graph_store,
        )

        self.assertEqual(summary["processed"], 1)
        self.assertGreaterEqual(summary["weak_signals"], 1)
        self.assertGreaterEqual(summary["references"], 1)

    def test_document_co_occurrence_ignores_non_person_like_noise(self) -> None:
        item = {
            "id": "noisy-1",
            "title": "Graph Benchmark Top Cases",
            "filename": "graph-benchmark-top-cases.md",
            "file_type": "md",
            "handler_type": "text",
            "source_type": "local",
            "original_path": "/tmp/graph-benchmark-top-cases.md",
            "preview_text": "Graph Top Accepted Misses",
            "summary": "Graph Top Accepted Misses repeated in benchmark notes.",
            "extracted_text": (
                "Graph Top Accepted Misses. Graph Top Accepted Misses. "
                "Graph Top Accepted Misses. This document is intentionally long enough "
                "to exceed two hundred characters so the co-occurrence extractor runs."
            ),
            "keywords": '["graph","benchmark"]',
        }

        result = extract_document_co_occurrences(item, self.graph_store)

        self.assertEqual(result["weak_signals"], 0)


if __name__ == "__main__":
    unittest.main()
