from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from backend.cognition.store import CognitiveStore


class CognitiveStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "cognitive_layer.db"
        self.store = CognitiveStore(self.db_path)

    def test_record_activation_event_persists_history(self) -> None:
        self.store.record_activation_event(
            "item-1",
            "open",
            occurred_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
        )

        history = self.store.get_activation_history("item-1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0][1], 1.0)

    def test_base_level_activation_uses_saved_history(self) -> None:
        self.store.record_activation_event(
            "item-1",
            "open",
            occurred_at=datetime(2026, 4, 2, 11, 50, tzinfo=timezone.utc),
        )
        self.store.record_activation_event(
            "item-1",
            "open",
            occurred_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.store.record_activation_event(
            "item-old",
            "open",
            occurred_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
        )

        score = self.store.get_base_level_activation(
            "item-1",
            now=datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
        )
        old_score = self.store.get_base_level_activation(
            "item-old",
            now=datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
        )

        self.assertGreater(score, old_score)

    def test_upsert_frame_round_trips_slots(self) -> None:
        self.store.upsert_frame_instance(
            "item-1",
            "proposal",
            {"recipient": "Acme", "status": "draft"},
            confidence=0.82,
        )

        frame = self.store.get_frame_instance("item-1")
        self.assertIsNotNone(frame)
        self.assertEqual(frame["frame_type"], "proposal")
        self.assertEqual(frame["slots"]["recipient"], "Acme")
        self.assertAlmostEqual(frame["confidence"], 0.82, places=2)

    def test_entity_edge_upsert_updates_weight(self) -> None:
        self.store.upsert_entity_edge("entity:a", "entity:b", weight=0.4)
        self.store.upsert_entity_edge("entity:a", "entity:b", weight=0.9)

        related = self.store.get_related_entities("entity:a")
        self.assertEqual(len(related), 1)
        self.assertAlmostEqual(float(related[0]["weight"]), 0.9, places=3)

    def test_counts_reflect_saved_rows(self) -> None:
        self.store.record_activation_event("item-1", "open")
        self.store.upsert_entity_edge("entity:a", "entity:b", weight=0.5)
        self.store.upsert_frame_instance("item-1", "note", {"project": "Recurdo"})

        counts = self.store.get_counts()
        self.assertEqual(counts["activation_events"], 1)
        self.assertEqual(counts["entity_edges"], 1)
        self.assertEqual(counts["frame_instances"], 1)

    def test_production_log_round_trips(self) -> None:
        self.store.log_production_fire(
            "stalled_script",
            conditions={"active_script": "fundraising"},
            surfaced_items=[{"item_id": "item-1"}],
        )

        last_fired = self.store.get_last_production_fire("stalled_script")
        events = self.store.get_production_log(rule_id="stalled_script")
        self.assertIsNotNone(last_fired)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["conditions"]["active_script"], "fundraising")
        self.assertEqual(events[0]["surfaced_items"][0]["item_id"], "item-1")

    def test_script_state_round_trips(self) -> None:
        self.store.upsert_script_state(
            "fundraising",
            "outreach",
            entities_involved=["entity:investor", "entity:pitch-deck"],
        )

        states = self.store.get_script_states()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["script_type"], "fundraising")
        self.assertEqual(states[0]["current_scene"], "outreach")
        self.assertEqual(states[0]["entities_involved"][0], "entity:investor")


if __name__ == "__main__":
    unittest.main()
