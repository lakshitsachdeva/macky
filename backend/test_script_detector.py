from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from backend.cognition.scripts.detector import (
    compute_script_relevance,
    detect_script_context,
)
from backend.cognition.store import CognitiveStore


class ScriptDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = CognitiveStore(Path(self.temp_dir.name) / "cognitive_layer.db")
        self.now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)

        self.store.upsert_frame_instance(
            "pitch-1",
            "pitch_deck",
            {"title": "Recurdo Seed Deck"},
            confidence=0.9,
        )
        self.store.upsert_frame_instance(
            "email-1",
            "email",
            {"subject": "warm investor intro"},
            confidence=0.94,
        )
        self.store.upsert_frame_instance(
            "meeting-1",
            "meeting_notes",
            {"title": "Investor meeting notes"},
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
            occurred_at=datetime(2026, 3, 18, 10, 0, tzinfo=timezone.utc),
        )

    def test_detect_script_context_identifies_fundraising(self) -> None:
        context = detect_script_context(
            self.store,
            current_frame_type="email",
            current_query="warm investor intro",
            now=self.now,
        )

        self.assertEqual(context["active_script"], "fundraising")
        self.assertEqual(context["current_scene"], "outreach")
        self.assertEqual(context["next_scene"], "meetings")
        self.assertGreater(context["confidence"], 0.7)

    def test_generic_recent_documents_do_not_invent_active_script(self) -> None:
        store = CognitiveStore(Path(self.temp_dir.name) / "generic_docs.db")
        for index in range(3):
            item_id = f"doc-{index}"
            store.upsert_frame_instance(
                item_id,
                "document",
                {"title": f"Generic Document {index}"},
                confidence=0.8,
            )
            store.record_activation_event(
                item_id,
                "open",
                occurred_at=datetime(2026, 4, 9, 10, index, tzinfo=timezone.utc),
            )

        context = detect_script_context(store, now=self.now)

        self.assertIsNone(context["active_script"])
        self.assertEqual(context["confidence"], 0.0)

    def test_compute_script_relevance_prefers_scene_aligned_frame(self) -> None:
        context = {
            "active_script": "fundraising",
            "current_scene": "outreach",
            "next_scene": "meetings",
            "query_scene": None,
            "confidence": 0.8,
        }

        email_score = compute_script_relevance(
            frame_type="email",
            item={"title": "Warm intro email", "summary": "investor email"},
            script_context=context,
        )
        invoice_score = compute_script_relevance(
            frame_type="invoice",
            item={"title": "Invoice April"},
            script_context=context,
        )

        self.assertGreater(email_score, invoice_score)
        self.assertGreater(email_score, 0.5)


if __name__ == "__main__":
    unittest.main()
