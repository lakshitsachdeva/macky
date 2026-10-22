from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.cognition.production.rules import ProductionEngine
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
            original_path TEXT,
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
            original_path, first_seen_at, created_at, modified_at, last_opened_at,
            open_count, total_dwell_seconds, copy_event_count, is_excluded
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        [
            (
                "invoice-1",
                "invoice-feb.pdf",
                "Invoice February",
                "pdf",
                "text",
                "file",
                "Acme Studio",
                "accounts@acme.test",
                "Invoice #1234 Bill To XYZ Corp Amount Due ₹50,000 Payment due March 1, 2026",
                "Invoice #1234 Bill To XYZ Corp Amount Due ₹50,000 Payment due March 1, 2026",
                "Invoice for February work",
                '["invoice","billing"]',
                "/tmp/invoice-feb.pdf",
                "2026-02-01T10:00:00+00:00",
                "2026-02-01T10:00:00+00:00",
                "2026-02-01T10:00:00+00:00",
                "2026-02-01T10:00:00+00:00",
                1,
                120,
                0,
            ),
            (
                "email-1",
                "investor-email.eml",
                "Subject: follow up on deck",
                "text",
                "text",
                "gmail",
                "Rahul Mehta",
                "rahul@example.com",
                "Can you send the latest investor deck?",
                "Subject: follow up on deck From: Rahul Mehta Can you send the latest investor deck?",
                "Investor follow-up email",
                '["investor","email"]',
                "/tmp/investor-email.eml",
                "2026-03-18T12:00:00+00:00",
                "2026-03-18T12:00:00+00:00",
                "2026-03-18T12:00:00+00:00",
                "2026-03-18T12:00:00+00:00",
                2,
                240,
                0,
            ),
            (
                "pitch-1",
                "seed-deck.pptx",
                "Recurdo Seed Deck",
                "presentation",
                "text",
                "file",
                None,
                None,
                "pitch deck investor deck seed round market opportunity",
                "pitch deck investor deck seed round market opportunity",
                "Fundraising deck",
                '["fundraising","seed round"]',
                "/tmp/seed-deck.pptx",
                "2026-03-15T10:00:00+00:00",
                "2026-03-15T10:00:00+00:00",
                "2026-03-15T10:00:00+00:00",
                "2026-03-20T10:00:00+00:00",
                3,
                600,
                0,
            ),
            (
                "meeting-1",
                "meeting-notes.txt",
                "Investor Meeting Notes",
                "text",
                "text",
                "file",
                None,
                None,
                "meeting notes action items due diligence next steps",
                "meeting notes action items due diligence next steps",
                "Meeting with investor",
                '["meeting","investor"]',
                "/tmp/meeting-notes.txt",
                "2026-03-25T10:00:00+00:00",
                "2026-03-25T10:00:00+00:00",
                "2026-03-25T10:00:00+00:00",
                "2026-03-25T10:00:00+00:00",
                1,
                90,
                0,
            ),
        ],
    )
    conn.commit()
    conn.close()


def _insert_memory_item(
    path: Path,
    *,
    item_id: str,
    title: str,
    filename: str,
    original_path: str,
    timestamp: str,
    is_excluded: int = 0,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO items (
            id, filename, title, file_type, handler_type, source_type,
            sender_name, sender_email, preview_text, extracted_text, summary, keywords,
            original_path, first_seen_at, created_at, modified_at, last_opened_at,
            open_count, total_dwell_seconds, copy_event_count, is_excluded
        )
        VALUES (?, ?, ?, 'pdf', 'text', 'local', NULL, NULL, '', '', '', '[]', ?, ?, ?, ?, ?, 0, 0, 0, ?)
        """,
        (
            item_id,
            filename,
            title,
            original_path,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
            is_excluded,
        ),
    )
    conn.commit()
    conn.close()


class ProductionRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.memory_db = Path(self.temp_dir.name) / "memory.db"
        self.cognitive_db = Path(self.temp_dir.name) / "cognitive_layer.db"
        _create_memory_db(self.memory_db)
        self.store = CognitiveStore(self.cognitive_db)
        self.now_ref = [datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)]
        self.engine = ProductionEngine(
            self.store,
            memory_db_path=self.memory_db,
            current_time_fn=lambda: self.now_ref[0],
        )

        self.store.upsert_frame_instance(
            "invoice-1",
            "invoice",
            {
                "status": "unpaid",
                "amount": "₹50,000",
                "due_date": "March 1, 2026",
                "invoice_number": "1234",
            },
            confidence=0.92,
        )
        self.store.upsert_frame_instance(
            "email-1",
            "email",
            {
                "sender": "Rahul Mehta",
                "requires_response": "yes",
                "subject": "follow up on deck",
            },
            confidence=0.95,
        )
        self.store.upsert_frame_instance(
            "pitch-1",
            "pitch_deck",
            {"title": "Recurdo Seed Deck"},
            confidence=0.9,
        )
        self.store.upsert_frame_instance(
            "meeting-1",
            "meeting_notes",
            {"title": "Investor Meeting Notes"},
            confidence=0.84,
        )
        self.store.record_activation_event(
            "pitch-1",
            "open",
            occurred_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
        )
        self.store.record_activation_event(
            "email-1",
            "open",
            occurred_at=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
        )

    def test_stalled_script_rule_fires(self) -> None:
        fired = self.engine.evaluate(
            {
                "active_script": "fundraising",
                "script_name": "Fundraising",
                "days_since_script_activity": 20,
                "days": 20,
            }
        )

        rule_ids = {rule["rule_id"] for rule in fired}
        self.assertIn("stalled_script", rule_ids)

    def test_rule_cooldown_blocks_repeat_fire(self) -> None:
        context = {
            "active_script": "fundraising",
            "script_name": "Fundraising",
            "days_since_script_activity": 20,
            "days": 20,
        }

        first = self.engine.evaluate(context)
        second = self.engine.evaluate(context)

        self.assertTrue(any(rule["rule_id"] == "stalled_script" for rule in first))
        self.assertFalse(any(rule["rule_id"] == "stalled_script" for rule in second))

    def test_incomplete_frame_surface_fires_for_old_unpaid_invoice(self) -> None:
        context = self.engine.build_context(current_item_id="invoice-1")
        fired = self.engine.evaluate(context)
        rule_ids = {rule["rule_id"] for rule in fired}

        self.assertIn("incomplete_frame_surface", rule_ids)

    def test_build_context_detects_active_script_and_next_scene(self) -> None:
        context = self.engine.build_context(current_item_id="email-1")

        self.assertEqual(context["active_script"], "fundraising")
        self.assertEqual(context["current_scene"], "outreach")
        self.assertEqual(context["next_scene"], "meetings")
        self.assertGreaterEqual(context["days_since_script_activity"], 20)

    def test_build_context_collects_unanswered_emails(self) -> None:
        context = self.engine.build_context(current_item_id="email-1")

        self.assertGreaterEqual(len(context["unanswered_emails"]), 1)
        self.assertEqual(context["unanswered_emails"][0]["sender"], "Rahul Mehta")

    def test_anniversary_hits_require_actual_prior_year_date(self) -> None:
        self.now_ref[0] = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
        _insert_memory_item(
            self.memory_db,
            item_id="today-file",
            title="Experiment No. 9 Cyber Security 1",
            filename="B102_Exp9_CS.pdf",
            original_path="/tmp/B102_Exp9_CS.pdf",
            timestamp="2026-04-07T09:09:10+00:00",
        )

        context = self.engine.build_context()

        self.assertEqual(context["anniversary_hits"], [])

    def test_anniversary_hits_ignore_excluded_and_dedupe_paths(self) -> None:
        self.now_ref[0] = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
        _insert_memory_item(
            self.memory_db,
            item_id="last-year-file",
            title="Launch Notes",
            filename="launch-notes.pdf",
            original_path="/tmp/launch-notes.pdf",
            timestamp="2025-04-07T09:09:10+00:00",
        )
        _insert_memory_item(
            self.memory_db,
            item_id="last-year-duplicate",
            title="Launch Notes",
            filename="launch-notes-copy.pdf",
            original_path="/tmp/launch-notes.pdf",
            timestamp="2025-04-07T10:09:10+00:00",
        )
        _insert_memory_item(
            self.memory_db,
            item_id="last-year-excluded",
            title="Excluded Launch Notes",
            filename="excluded-launch-notes.pdf",
            original_path="/tmp/excluded-launch-notes.pdf",
            timestamp="2025-04-07T11:09:10+00:00",
            is_excluded=1,
        )

        context = self.engine.build_context()

        self.assertEqual(len(context["anniversary_hits"]), 1)
        self.assertEqual(context["anniversary_hits"][0]["item_id"], "last-year-duplicate")


if __name__ == "__main__":
    unittest.main()
