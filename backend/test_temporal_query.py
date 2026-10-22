from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import sqlite3

from backend.graph.bootstrap import bootstrap_memory_db
from backend.graph.store import GraphStore
from backend.retrieval.temporal import TemporalQueryEngine
from backend.test_graph_bootstrap import _create_memory_db


class TemporalQueryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.memory_db = Path(self.temp_dir.name) / "memory.db"
        self.graph_db = Path(self.temp_dir.name) / "graph.db"
        _create_memory_db(self.memory_db)
        self._move_fixture_items_into_last_week()
        bootstrap_memory_db(self.memory_db, graph_db_path=self.graph_db, reset=True)
        self.engine = TemporalQueryEngine(
            memory_db_path=self.memory_db,
            graph_store=GraphStore(self.graph_db),
        )

    def _move_fixture_items_into_last_week(self) -> None:
        """Keep relative-date tests stable as the calendar moves forward."""
        now = datetime.now(timezone.utc)
        timestamps = {
            "email-1": now - timedelta(days=6),
            "proposal-1": now - timedelta(days=5),
            "deck-1": now - timedelta(days=4),
        }
        conn = sqlite3.connect(self.memory_db)
        try:
            for item_id, timestamp in timestamps.items():
                value = timestamp.isoformat()
                conn.execute(
                    """
                    UPDATE items
                    SET first_seen_at = ?, created_at = ?, modified_at = ?, last_opened_at = ?
                    WHERE id = ?
                    """,
                    (value, value, value, value, item_id),
                )
            conn.commit()
        finally:
            conn.close()

    def test_query_period_returns_graph_entities_and_ranked_episodes(self) -> None:
        result = self.engine.query_period("fundraising last week", limit=5)

        self.assertEqual(result["query"], "fundraising last week")
        self.assertEqual(result["mode"], "activity")
        self.assertGreaterEqual(len(result["episodes"]), 1)
        self.assertTrue(
            any("Recurdo Seed Deck" in episode["title"] for episode in result["episodes"])
        )
        self.assertTrue(
            any(
                "fundraising" in entity["entity"].casefold()
                or "recurdo" in entity["entity"].casefold()
                for entity in result["entities"]
            )
        )

    def test_query_period_routes_document_queries_to_document_mode(self) -> None:
        result = self.engine.query_period("files from last week", limit=5)

        self.assertEqual(result["mode"], "document")
        self.assertGreaterEqual(len(result["episodes"]), 1)

    def test_query_period_routes_state_queries_to_state_mode(self) -> None:
        project_id = self.engine.graph_store.upsert_entity(name="Recurdo", entity_type="Project")
        status_id = self.engine.graph_store.upsert_entity(name="building", entity_type="Concept")
        self.engine.graph_store.add_temporal_fact(
            subject_id=project_id,
            predicate="HAS_STATUS",
            object_id=status_id,
            valid_from=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
            episode_id="deck-1",
            confidence=0.9,
        )

        result = self.engine.query_period("status of Recurdo last week", limit=5)

        self.assertEqual(result["mode"], "state")
        self.assertEqual(result["entity"]["name"], "Recurdo")
        self.assertIn("HAS_STATUS", result["state"])
        self.assertIn("building", result["state"]["HAS_STATUS"])

    def test_get_idea_evolution_returns_mentions_in_order(self) -> None:
        result = self.engine.get_idea_evolution("Rahul Sharma", limit=10)

        self.assertIsNotNone(result["entity"])
        self.assertGreaterEqual(len(result["episodes"]), 1)
        self.assertGreaterEqual(len(result["timeline"]), 1)
        self.assertEqual(result["entity"]["name"], "Rahul Sharma")
        self.assertEqual(result["timeline"][0]["predicate"], "SENT_TO")


if __name__ == "__main__":
    unittest.main()
