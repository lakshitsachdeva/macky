from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend import database
from backend.cognition.store import CognitiveStore
from backend.connectors.gmail import cleanup_gmail_items, parse_gmail_message, sync_gmail
from backend.graph.store import GraphStore


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _gmail_message(
    *,
    message_id: str = "msg-1",
    thread_id: str = "thread-1",
    subject: str = "Seed Deck Follow Up",
    snippet: str = "Quick follow up on the seed deck",
    label_ids: list[str] | None = None,
    extra_headers: list[dict[str, str]] | None = None,
    attachment_filenames: list[str] | None = None,
) -> dict[str, object]:
    internal_date = int(datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    plain_text = "Hello Lakshit,\nCan you review the seed deck this week?"
    html_text = "<p>Hello Lakshit,</p><p>Can you review the <b>seed deck</b> this week?</p>"
    attachment_parts = [
        {
            "mimeType": "application/octet-stream",
            "filename": filename,
            "body": {"attachmentId": f"att-{index}", "size": 128},
        }
        for index, filename in enumerate(attachment_filenames or [], start=1)
    ]
    return {
        "id": message_id,
        "threadId": thread_id,
        "historyId": "history-101",
        "internalDate": str(internal_date),
        "snippet": snippet,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "Rahul Sharma <rahul@acme.com>"},
                {"name": "To", "value": "Lakshit <lakshit@example.com>"},
                {"name": "Cc", "value": "Investors <investors@acme.com>"},
                {"name": "Date", "value": "Mon, 6 Apr 2026 17:30:00 +0530"},
                *(extra_headers or []),
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url(plain_text)}},
                {"mimeType": "text/html", "body": {"data": _b64url(html_text)}},
                *attachment_parts,
            ],
        },
        "labelIds": list(label_ids or ["INBOX", "CATEGORY_PERSONAL"]),
    }


class _FakeGmailConnector:
    def __init__(self, message: dict[str, object]) -> None:
        self.client_secrets_path = Path("/tmp/client_secrets.json")
        self.message = message
        self.sync_state = {
            "account_email": "founder@example.com",
            "last_history_id": "",
            "last_sync_at": None,
            "connected_at": "2026-04-06T10:00:00+00:00",
            "last_summary": {},
        }
        self.full_sync_queries: list[str] = []
        self.attachment_payloads: dict[str, bytes] = {}

    def client_configured(self) -> bool:
        return True

    def get_service(self, *, refresh: bool = True) -> object:
        del refresh
        return object()

    def get_profile(self, *, service: object | None = None) -> dict[str, str]:
        del service
        return {
            "emailAddress": "founder@example.com",
            "historyId": "history-202",
        }

    def load_sync_state(self) -> dict[str, object]:
        return dict(self.sync_state)

    def save_sync_state(self, updates: dict[str, object]) -> dict[str, object]:
        self.sync_state.update(updates)
        return dict(self.sync_state)

    def iter_full_sync_message_ids(
        self,
        *,
        service: object | None = None,
        query: str = "",
        max_messages: int | None = None,
    ) -> list[str]:
        del service
        self.full_sync_queries.append(query)
        message_ids = [str(self.message["id"])]
        return message_ids if max_messages is None else message_ids[:max_messages]

    def iter_incremental_message_ids(
        self,
        start_history_id: str,
        *,
        service: object | None = None,
    ) -> list[str]:
        del start_history_id, service
        return []

    def get_message(self, message_id: str, *, service: object | None = None) -> dict[str, object]:
        del service
        if message_id != self.message["id"]:
            raise AssertionError(f"Unexpected message id: {message_id}")
        return dict(self.message)

    def get_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        service: object | None = None,
    ) -> dict[str, object]:
        del service
        if message_id != self.message["id"]:
            raise AssertionError(f"Unexpected message id: {message_id}")
        payload = self.attachment_payloads.get(
            attachment_id,
            f"Attachment notes for {attachment_id}".encode("utf-8"),
        )
        return {"data": base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")}


class GmailConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base_path = Path(self.temp_dir.name)
        self.memory_db = base_path / "memory.db"
        self.graph_store = GraphStore(base_path / "graph.db")
        self.cognitive_store = CognitiveStore(base_path / "cognitive_layer.db")
        with patch.object(database, "DB_PATH", self.memory_db):
            database.init_db()

    def test_parse_gmail_message_extracts_headers_and_body(self) -> None:
        parsed = parse_gmail_message(
            _gmail_message(attachment_filenames=["seed-deck.pdf"]),
            account_email="founder@example.com",
        )

        self.assertEqual(parsed["message_id"], "msg-1")
        self.assertEqual(parsed["thread_id"], "thread-1")
        self.assertEqual(parsed["subject"], "Seed Deck Follow Up")
        self.assertEqual(parsed["sender_email"], "rahul@acme.com")
        self.assertIn("Lakshit", parsed["recipient_labels"])
        self.assertIn("Hello Lakshit,", parsed["body_text"])
        self.assertIn("Subject: Seed Deck Follow Up", parsed["extracted_text"])
        self.assertIn("Attachments: seed-deck.pdf", parsed["extracted_text"])
        self.assertIn("INBOX", parsed["label_ids"])
        self.assertEqual(parsed["attachment_count"], 1)
        self.assertIn("seed-deck.pdf", parsed["attachment_names"])
        self.assertIn("has_attachment", parsed["keywords"])
        self.assertTrue(str(parsed["original_path"]).startswith("https://mail.google.com/mail/u/0/#all/"))

    def test_sync_gmail_ingests_and_updates_message_knowledge(self) -> None:
        connector = _FakeGmailConnector(_gmail_message(attachment_filenames=["seed-deck.pdf"]))
        knowledge_first = {
            "provider": "heuristic",
            "anchor_time": "2026-04-06T12:00:00+00:00",
            "frame": {
                "frame_type": "email",
                "slots": {
                    "contact": "Rahul Sharma",
                    "status": "awaiting_reply",
                    "topic": "Seed Deck",
                },
                "confidence": 0.84,
            },
            "graph_entities": [
                {"name": "Rahul Sharma", "entity_type": "Person", "confidence": 0.92},
                {"name": "Seed Deck", "entity_type": "Document", "confidence": 0.88},
            ],
            "cognitive_entities": [
                ("entity:rahul-sharma", 0.86),
                ("entity:seed-deck", 0.78),
            ],
            "temporal_facts": [
                {
                    "subject": "Rahul Sharma",
                    "subject_type": "Person",
                    "predicate": "RELATED_TO",
                    "object": "Seed Deck",
                    "object_type": "Document",
                    "valid_from": "2026-04-06T12:00:00+00:00",
                    "episode_id": "ignored",
                    "confidence": 0.8,
                }
            ],
            "keywords": ["seed deck", "rahul sharma"],
        }
        knowledge_second = {
            "provider": "heuristic",
            "anchor_time": "2026-04-06T12:05:00+00:00",
            "frame": {
                "frame_type": "email",
                "slots": {
                    "contact": "Rahul Sharma",
                    "status": "replied",
                    "topic": "Investor Update",
                },
                "confidence": 0.9,
            },
            "graph_entities": [
                {"name": "Rahul Sharma", "entity_type": "Person", "confidence": 0.94},
                {"name": "Investor Update", "entity_type": "Document", "confidence": 0.9},
            ],
            "cognitive_entities": [
                ("entity:rahul-sharma", 0.87),
                ("entity:investor-update", 0.8),
            ],
            "temporal_facts": [
                {
                    "subject": "Rahul Sharma",
                    "subject_type": "Person",
                    "predicate": "RELATED_TO",
                    "object": "Investor Update",
                    "object_type": "Document",
                    "valid_from": "2026-04-06T12:05:00+00:00",
                    "episode_id": "ignored",
                    "confidence": 0.85,
                }
            ],
            "keywords": ["investor update", "rahul sharma"],
        }
        progress_events: list[dict[str, object]] = []

        with (
            patch(
                "backend.connectors.gmail.embedder.embed_and_index_text",
                return_value={"keywords": ["email", "seed deck"]},
            ),
            patch(
                "backend.connectors.gmail.enricher.enrich_item",
                new=AsyncMock(
                    return_value={
                        "summary": "Quick follow up on the seed deck",
                        "doc_type": "email",
                        "importance_score": 0.72,
                    }
                ),
            ),
            patch(
                "backend.connectors.gmail.extract_item_knowledge",
                new=AsyncMock(side_effect=[knowledge_first, knowledge_second]),
            ),
        ):
            first_summary = sync_gmail(
                connector=connector,
                memory_db_path=self.memory_db,
                graph_store=self.graph_store,
                cognitive_store=self.cognitive_store,
                full_sync=True,
                progress_callback=progress_events.append,
            )
            second_summary = sync_gmail(
                connector=connector,
                memory_db_path=self.memory_db,
                graph_store=self.graph_store,
                cognitive_store=self.cognitive_store,
                full_sync=True,
            )

        self.assertEqual(first_summary["mode"], "full")
        self.assertEqual(first_summary["processed"], 1)
        self.assertEqual(first_summary["created"], 1)
        self.assertEqual(first_summary["updated"], 0)
        self.assertEqual(first_summary["indexed_items"], 1)
        self.assertEqual(second_summary["processed"], 1)
        self.assertEqual(second_summary["created"], 0)
        self.assertEqual(second_summary["updated"], 1)
        self.assertEqual(second_summary["indexed_items"], 1)
        self.assertEqual(str(connector.sync_state["last_history_id"]), "history-202")
        self.assertTrue(any(event.get("state") == "completed" for event in progress_events))

        conn = sqlite3.connect(self.memory_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT id, source_type, title, summary, keywords, doc_type_guess
                FROM items
                WHERE source_type = 'gmail'
                """
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM items WHERE source_type = 'gmail'"
            ).fetchone()["count"]
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(count, 1)
        self.assertEqual(row["source_type"], "gmail")
        self.assertEqual(row["title"], "Seed Deck Follow Up")
        self.assertEqual(row["doc_type_guess"], "email")
        self.assertEqual(row["summary"], "Quick follow up on the seed deck")
        keywords = json.loads(row["keywords"])
        self.assertIn("investor update", keywords)
        self.assertIn("has_attachment", keywords)

        item_id = str(row["id"])
        frame = self.cognitive_store.get_frame_instance(item_id)
        counts = self.cognitive_store.get_counts()
        episode_entities = self.graph_store.get_episode_entities([item_id])[item_id]
        entity_names = {entity["name"] for entity in episode_entities}
        timeline = self.graph_store.get_entity_timeline("Rahul Sharma")

        self.assertEqual(frame["frame_type"], "email")
        self.assertEqual(frame["slots"]["status"], "replied")
        self.assertEqual(frame["slots"]["topic"], "Investor Update")
        self.assertEqual(counts["activation_events"], 1)
        self.assertGreater(counts["entity_edges"], 0)
        self.assertIn("Rahul Sharma", entity_names)
        self.assertIn("Investor Update", entity_names)
        self.assertNotIn("Seed Deck", entity_names)
        self.assertTrue(any(entry["object"] == "Investor Update" for entry in timeline))

    def test_sync_gmail_skips_promotional_messages(self) -> None:
        connector = _FakeGmailConnector(
            _gmail_message(
                subject="Limited Time Offer",
                snippet="Unsubscribe from weekly offers",
                label_ids=["INBOX", "CATEGORY_PROMOTIONS"],
                extra_headers=[{"name": "List-Unsubscribe", "value": "<mailto:unsubscribe@example.com>"}],
            )
        )

        summary = sync_gmail(
            connector=connector,
            memory_db_path=self.memory_db,
            graph_store=self.graph_store,
            cognitive_store=self.cognitive_store,
            full_sync=True,
        )

        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["indexed_items"], 0)

    def test_sync_gmail_uses_safe_bootstrap_window_without_history_cursor(self) -> None:
        connector = _FakeGmailConnector(_gmail_message())
        knowledge = {
            "provider": "heuristic",
            "anchor_time": "2026-04-06T12:00:00+00:00",
            "frame": None,
            "graph_entities": [],
            "cognitive_entities": [],
            "temporal_facts": [],
            "keywords": ["email"],
        }

        with (
            patch(
                "backend.connectors.gmail.embedder.embed_and_index_text",
                return_value={"keywords": ["email"]},
            ),
            patch(
                "backend.connectors.gmail.enricher.enrich_item",
                new=AsyncMock(
                    return_value={
                        "summary": "Quick follow up on the seed deck",
                        "doc_type": "email",
                        "importance_score": 0.72,
                    }
                ),
            ),
            patch(
                "backend.connectors.gmail.extract_item_knowledge",
                new=AsyncMock(return_value=knowledge),
            ),
        ):
            summary = sync_gmail(
                connector=connector,
                memory_db_path=self.memory_db,
                graph_store=self.graph_store,
                cognitive_store=self.cognitive_store,
            )

        self.assertEqual(summary["mode"], "bootstrap")
        self.assertEqual(summary["historical_days"], 30)
        self.assertTrue(connector.full_sync_queries)
        self.assertIn("after:", connector.full_sync_queries[-1])
        self.assertNotIn("has:attachment", connector.full_sync_queries[-1])

    def test_sync_gmail_can_require_attachments(self) -> None:
        connector = _FakeGmailConnector(_gmail_message())

        summary = sync_gmail(
            connector=connector,
            memory_db_path=self.memory_db,
            graph_store=self.graph_store,
            cognitive_store=self.cognitive_store,
            historical_days=30,
            require_attachments=True,
        )

        self.assertEqual(summary["mode"], "historical")
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["historical_days"], 30)
        self.assertTrue(summary["require_attachments"])
        self.assertTrue(connector.full_sync_queries)
        self.assertIn("after:", connector.full_sync_queries[-1])
        self.assertIn("has:attachment", connector.full_sync_queries[-1])

    def test_sync_gmail_indexes_supported_attachments_as_separate_items(self) -> None:
        connector = _FakeGmailConnector(_gmail_message(attachment_filenames=["seed-deck.txt"]))
        connector.attachment_payloads["att-1"] = b"Seed deck attachment\nBudget: 250000\nNext step: partner meeting"
        email_knowledge = {
            "provider": "heuristic",
            "anchor_time": "2026-04-06T12:00:00+00:00",
            "frame": {
                "frame_type": "email",
                "slots": {"contact": "Rahul Sharma"},
                "confidence": 0.84,
            },
            "graph_entities": [
                {"name": "Rahul Sharma", "entity_type": "Person", "confidence": 0.92},
            ],
            "cognitive_entities": [("entity:rahul-sharma", 0.86)],
            "temporal_facts": [],
            "keywords": ["rahul sharma"],
        }
        attachment_knowledge = {
            "provider": "heuristic",
            "anchor_time": "2026-04-06T12:00:00+00:00",
            "frame": {
                "frame_type": "pitch_deck",
                "slots": {"project": "Seed Deck"},
                "confidence": 0.86,
            },
            "graph_entities": [
                {"name": "Seed Deck", "entity_type": "Document", "confidence": 0.9},
            ],
            "cognitive_entities": [("entity:seed-deck", 0.84)],
            "temporal_facts": [],
            "keywords": ["seed deck", "partner meeting"],
        }

        with (
            patch(
                "backend.connectors.gmail.embedder.embed_and_index_text",
                return_value={"keywords": ["seed deck"]},
            ),
            patch(
                "backend.connectors.gmail.enricher.enrich_item",
                new=AsyncMock(
                    side_effect=[
                        {
                            "summary": "Quick follow up on the seed deck",
                            "doc_type": "email",
                            "importance_score": 0.72,
                        },
                        {
                            "summary": "Seed deck attachment with partner meeting next step",
                            "doc_type": "pitch_deck",
                            "importance_score": 0.76,
                        },
                    ]
                ),
            ),
            patch(
                "backend.connectors.gmail.extract_item_knowledge",
                new=AsyncMock(side_effect=[email_knowledge, attachment_knowledge]),
            ),
        ):
            summary = sync_gmail(
                connector=connector,
                memory_db_path=self.memory_db,
                graph_store=self.graph_store,
                cognitive_store=self.cognitive_store,
                historical_days=30,
                require_attachments=True,
            )

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["attachments_processed"], 1)
        self.assertEqual(summary["attachments_created"], 1)
        self.assertEqual(summary["attachments_skipped"], 0)
        self.assertEqual(summary["indexed_items"], 2)

        conn = sqlite3.connect(self.memory_db)
        conn.row_factory = sqlite3.Row
        try:
            attachment = conn.execute(
                """
                SELECT id, source_type, title, filename, original_path, extracted_text, keywords
                FROM items
                WHERE source_type = 'gmail_attachment'
                """
            ).fetchone()
            active_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM items
                WHERE COALESCE(is_excluded, 0) = 0
                  AND source_type IN ('gmail', 'gmail_attachment')
                """
            ).fetchone()["count"]
        finally:
            conn.close()

        self.assertIsNotNone(attachment)
        self.assertEqual(active_count, 2)
        self.assertEqual(attachment["filename"], "seed-deck.txt")
        self.assertIn("recurdo_attachment=att-1", attachment["original_path"])
        self.assertIn("Budget: 250000", attachment["extracted_text"])
        self.assertIn("partner meeting", json.loads(attachment["keywords"]))
        frame = self.cognitive_store.get_frame_instance(str(attachment["id"]))
        self.assertEqual(frame["frame_type"], "pitch_deck")

    def test_cleanup_gmail_items_retires_messages_and_clears_knowledge(self) -> None:
        connector = _FakeGmailConnector(_gmail_message())
        knowledge = {
            "provider": "heuristic",
            "anchor_time": "2026-04-06T12:00:00+00:00",
            "frame": {
                "frame_type": "email",
                "slots": {"contact": "Rahul Sharma"},
                "confidence": 0.84,
            },
            "graph_entities": [
                {"name": "Rahul Sharma", "entity_type": "Person", "confidence": 0.92},
            ],
            "cognitive_entities": [("entity:rahul-sharma", 0.86)],
            "temporal_facts": [],
            "keywords": ["rahul sharma"],
        }

        with (
            patch(
                "backend.connectors.gmail.embedder.embed_and_index_text",
                return_value={"keywords": ["email", "seed deck"]},
            ),
            patch(
                "backend.connectors.gmail.enricher.enrich_item",
                new=AsyncMock(
                    return_value={
                        "summary": "Quick follow up on the seed deck",
                        "doc_type": "email",
                        "importance_score": 0.72,
                    }
                ),
            ),
            patch(
                "backend.connectors.gmail.extract_item_knowledge",
                new=AsyncMock(return_value=knowledge),
            ),
        ):
            sync_gmail(
                connector=connector,
                memory_db_path=self.memory_db,
                graph_store=self.graph_store,
                cognitive_store=self.cognitive_store,
                full_sync=True,
            )

        summary = cleanup_gmail_items(
            connector=connector,
            memory_db_path=self.memory_db,
            graph_store=self.graph_store,
            cognitive_store=self.cognitive_store,
        )

        self.assertEqual(summary["retired"], 1)
        self.assertEqual(summary["indexed_items"], 0)

        conn = sqlite3.connect(self.memory_db)
        conn.row_factory = sqlite3.Row
        try:
            item = conn.execute(
                "SELECT is_excluded FROM items WHERE source_type = 'gmail' LIMIT 1"
            ).fetchone()
            embedding_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        finally:
            conn.close()

        self.assertIsNotNone(item)
        self.assertEqual(int(item["is_excluded"] or 0), 1)
        self.assertEqual(int(embedding_count or 0), 0)
        self.assertEqual(self.cognitive_store.get_counts()["frame_instances"], 0)
        self.assertEqual(self.cognitive_store.get_counts()["activation_events"], 0)
        self.assertEqual(self.graph_store.get_counts()["episode_mentions"], 0)


if __name__ == "__main__":
    unittest.main()
