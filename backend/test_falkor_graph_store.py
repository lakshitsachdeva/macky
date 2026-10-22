from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from backend.graph.falkor_store import FalkorGraphStore, is_falkor_available
from backend.graph.bootstrap import bootstrap_memory_db
from backend.test_graph_bootstrap import _create_memory_db


@unittest.skipUnless(is_falkor_available(), "falkordblite requires Python 3.12+")
class FalkorGraphStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.graph_db = Path(self.temp_dir.name) / "graph.db"
        self.store = FalkorGraphStore(self.graph_db, graph_name="recurdo_test")
        self.addCleanup(self.store.clear_all)

    def test_upsert_entity_merges_aliases(self) -> None:
        first_id = self.store.upsert_entity(
            name="Rahul Sharma",
            entity_type="Person",
            aliases=["rahul@acme.com"],
        )
        second_id = self.store.upsert_entity(
            name="rahul@acme.com",
            entity_type="Person",
        )

        self.assertEqual(first_id, second_id)
        self.assertEqual(self.store.get_counts()["entities"], 1)
        entity = self.store.get_entity_by_name("Rahul Sharma")
        self.assertIsNotNone(entity)
        assert entity is not None
        self.assertIn("rahul@acme.com", entity["aliases"])

    def test_singleton_temporal_fact_invalidation(self) -> None:
        project_id = self.store.upsert_entity(name="Recurdo Fundraise", entity_type="Project")
        draft_id = self.store.upsert_entity(name="draft", entity_type="Concept")
        submitted_id = self.store.upsert_entity(name="submitted", entity_type="Concept")

        day_one = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
        day_five = datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc)

        self.store.add_temporal_fact(
            subject_id=project_id,
            predicate="HAS_STATUS",
            object_id=draft_id,
            valid_from=day_one,
            episode_id="item-1",
        )
        self.store.add_temporal_fact(
            subject_id=project_id,
            predicate="HAS_STATUS",
            object_id=submitted_id,
            valid_from=day_five,
            episode_id="item-2",
        )

        timeline = self.store.get_entity_timeline("Recurdo Fundraise")
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["object"], "draft")
        self.assertEqual(timeline[0]["valid_until"], day_five.isoformat())
        self.assertEqual(timeline[1]["object"], "submitted")

    def test_find_connections_walks_mentions_and_facts(self) -> None:
        alice_id = self.store.upsert_entity(name="Alice", entity_type="Person")
        bob_id = self.store.upsert_entity(name="Bob", entity_type="Person")
        carol_id = self.store.upsert_entity(name="Carol", entity_type="Person")

        self.store.link_episode_to_entity("episode-1", alice_id)
        self.store.link_episode_to_entity("episode-1", bob_id)
        self.store.link_episode_to_entity("episode-2", bob_id)
        self.store.link_episode_to_entity("episode-2", carol_id)

        connections = self.store.find_connections("Alice", depth=2)
        by_name = {entry["name"]: entry for entry in connections}

        self.assertIn("Bob", by_name)
        self.assertIn("Carol", by_name)
        self.assertEqual(by_name["Bob"]["hops"], 1)
        self.assertEqual(by_name["Carol"]["hops"], 2)

    def test_bootstrap_can_target_falkor_backend(self) -> None:
        memory_db = Path(self.temp_dir.name) / "memory.db"
        graph_db = Path(self.temp_dir.name) / "bootstrap-graph.db"
        _create_memory_db(memory_db)

        summary = bootstrap_memory_db(
            memory_db,
            graph_db_path=graph_db,
            graph_backend="falkor",
            reset=True,
        )
        store = FalkorGraphStore(graph_db)
        counts = store.get_counts()

        self.assertEqual(summary["items"], 3)
        self.assertGreater(summary["entities"], 4)
        self.assertGreater(summary["episode_mentions"], 4)
        self.assertGreater(summary["temporal_facts"], 0)
        self.assertEqual(counts["temporal_facts"], summary["temporal_facts"])


if __name__ == "__main__":
    unittest.main()
