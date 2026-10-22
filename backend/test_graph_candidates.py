from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from backend.graph.candidates import GraphCandidateGenerator
from backend.graph.store import GraphStore


class GraphCandidateGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.graph_store = GraphStore(Path(self.temp_dir.name) / "graph.db")
        self.generator = GraphCandidateGenerator(self.graph_store)

        rahul_id = self.graph_store.upsert_entity(name="Rahul Sharma", entity_type="Person")
        recurdo_id = self.graph_store.upsert_entity(name="Recurdo", entity_type="Project")
        investor_id = self.graph_store.upsert_entity(name="Investor Update", entity_type="Document")

        self.graph_store.link_episode_to_entity("episode-1", rahul_id)
        self.graph_store.link_episode_to_entity("episode-1", recurdo_id)
        self.graph_store.link_episode_to_entity("episode-2", recurdo_id)
        self.graph_store.link_episode_to_entity("episode-2", investor_id)
        self.graph_store.add_temporal_fact(
            subject_id=rahul_id,
            predicate="WORKS_ON",
            object_id=recurdo_id,
            valid_from="2026-04-01T10:00:00+00:00",
            episode_id="episode-1",
            confidence=0.95,
        )
        self.graph_store.add_temporal_fact(
            subject_id=recurdo_id,
            predicate="RELATES_TO",
            object_id=investor_id,
            valid_from="2026-04-02T10:00:00+00:00",
            episode_id="episode-2",
            confidence=0.82,
        )

    def test_extract_seeds_filters_generic_terms(self) -> None:
        seeds = self.generator.extract_seeds("project startup thing working on")
        self.assertEqual(seeds, {})

    def test_generate_returns_graph_candidates_for_real_seed(self) -> None:
        results = self.generator.generate("what was Rahul working on for Recurdo", n=5)

        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["source_pool"], "graph")
        self.assertGreater(float(results[0]["graph_score"]), 0.0)
        self.assertTrue(results[0]["graph_path"])
        self.assertIn(results[0]["episode_id"], {"episode-1", "episode-2"})


if __name__ == "__main__":
    unittest.main()
