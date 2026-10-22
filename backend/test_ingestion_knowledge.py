from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from backend.cognition.store import CognitiveStore
from backend.graph.store import GraphStore
from backend.llm.extraction import apply_item_knowledge


class IngestionKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base_path = Path(self.temp_dir.name)
        self.cognitive_store = CognitiveStore(base_path / "cognitive_layer.db")
        self.graph_store = GraphStore(base_path / "graph.db")
        self.item = {
            "id": "item-1",
            "title": "Proposal Draft",
            "filename": "proposal.pdf",
            "source_type": "local",
            "first_seen_at": "2026-04-03T10:00:00+00:00",
        }

    def test_apply_item_knowledge_persists_and_replaces_item_state(self) -> None:
        first_knowledge = {
            "provider": "heuristic",
            "anchor_time": "2026-04-03T10:00:00+00:00",
            "frame": {
                "frame_type": "proposal",
                "slots": {
                    "recipient": "Acme Corp",
                    "project_name": "Website Revamp",
                    "status": "submitted",
                },
                "confidence": 0.9,
            },
            "graph_entities": [
                {"name": "Acme Corp", "entity_type": "Organisation", "confidence": 0.92},
                {"name": "Website Revamp", "entity_type": "Project", "confidence": 0.9},
            ],
            "cognitive_entities": [
                ("entity:acme-corp", 0.82),
                ("entity:website-revamp", 0.79),
            ],
            "temporal_facts": [
                {
                    "subject": "Website Revamp",
                    "subject_type": "Project",
                    "predicate": "HAS_STATUS",
                    "object": "submitted",
                    "object_type": "Concept",
                    "valid_from": "2026-04-03T10:00:00+00:00",
                    "episode_id": "item-1",
                    "confidence": 0.9,
                }
            ],
        }

        second_knowledge = {
            "provider": "heuristic",
            "anchor_time": "2026-04-04T10:00:00+00:00",
            "frame": {
                "frame_type": "proposal",
                "slots": {
                    "recipient": "Beta Labs",
                    "project_name": "App Redesign",
                    "status": "accepted",
                },
                "confidence": 0.93,
            },
            "graph_entities": [
                {"name": "Beta Labs", "entity_type": "Organisation", "confidence": 0.94},
                {"name": "App Redesign", "entity_type": "Project", "confidence": 0.91},
            ],
            "cognitive_entities": [
                ("entity:beta-labs", 0.86),
                ("entity:app-redesign", 0.8),
            ],
            "temporal_facts": [
                {
                    "subject": "App Redesign",
                    "subject_type": "Project",
                    "predicate": "HAS_STATUS",
                    "object": "accepted",
                    "object_type": "Concept",
                    "valid_from": "2026-04-04T10:00:00+00:00",
                    "episode_id": "item-1",
                    "confidence": 0.92,
                }
            ],
        }

        first_summary = apply_item_knowledge(
            self.item,
            first_knowledge,
            graph_store=self.graph_store,
            cognitive_store=self.cognitive_store,
        )
        second_summary = apply_item_knowledge(
            self.item,
            second_knowledge,
            graph_store=self.graph_store,
            cognitive_store=self.cognitive_store,
        )

        frame = self.cognitive_store.get_frame_instance("item-1")
        counts = self.cognitive_store.get_counts()
        episode_entities = self.graph_store.get_episode_entities(["item-1"])["item-1"]
        entity_names = {entity["name"] for entity in episode_entities}
        timeline = self.graph_store.get_entity_timeline("App Redesign")

        self.assertEqual(first_summary["frame_type"], "proposal")
        self.assertEqual(second_summary["entities"], 2)
        self.assertIn("graph_context", second_summary)
        self.assertEqual(frame["slots"]["recipient"], "Beta Labs")
        self.assertEqual(frame["slots"]["status"], "accepted")
        self.assertEqual(counts["activation_events"], 1)
        self.assertGreater(counts["entity_edges"], 0)
        self.assertIn("Beta Labs", entity_names)
        self.assertIn("App Redesign", entity_names)
        self.assertNotIn("Acme Corp", entity_names)
        self.assertTrue(any(entry["predicate"] == "HAS_STATUS" and entry["object"] == "accepted" for entry in timeline))


if __name__ == "__main__":
    unittest.main()
