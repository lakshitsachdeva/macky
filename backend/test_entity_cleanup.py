from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.cognition import heuristics
from scripts import clean_entities


class GraphEntityFilterTests(unittest.TestCase):
    def test_valid_graph_entity_accepts_project_phrase(self) -> None:
        self.assertTrue(heuristics.is_valid_graph_entity("Swarm Alpha", "Project"))

    def test_valid_graph_entity_rejects_stopword(self) -> None:
        self.assertFalse(heuristics.is_valid_graph_entity("it", "Concept"))

    def test_valid_graph_entity_rejects_numeric(self) -> None:
        self.assertFalse(heuristics.is_valid_graph_entity("2024", "Concept"))

    def test_valid_graph_entity_rejects_pathlike_artifact(self) -> None:
        self.assertFalse(
            heuristics.is_valid_graph_entity("https://example.com/doc", "Concept")
        )


class EntityCleanupClassificationTests(unittest.TestCase):
    def _row(
        self,
        *,
        name: str,
        entity_type: str = "Concept",
        mention_edges: int = 0,
        fact_count: int = 0,
    ) -> dict[str, object]:
        return {
            "id": "entity-1",
            "name": name,
            "entity_type": entity_type,
            "mention_edges": mention_edges,
            "fact_count": fact_count,
        }

    def test_classify_stopword(self) -> None:
        reason = clean_entities._classify_entity(self._row(name="No", mention_edges=10))
        self.assertEqual(reason, "stopword/blocklist")

    def test_classify_two_char_without_facts(self) -> None:
        reason = clean_entities._classify_entity(self._row(name="xy", mention_edges=2))
        self.assertEqual(reason, "len==2-no-typed-facts")

    def test_preserve_allowed_short_entity(self) -> None:
        reason = clean_entities._classify_entity(self._row(name="AI", mention_edges=4))
        self.assertIsNone(reason)

    def test_classify_orphan(self) -> None:
        reason = clean_entities._classify_entity(self._row(name="UsefulTerm"))
        self.assertEqual(reason, "orphan")


class RebuildNetworkTests(unittest.TestCase):
    def test_rebuild_network_writes_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            memory_db = tmp / "memory.db"
            cognitive_db = tmp / "cognitive_layer.db"

            conn = sqlite3.connect(str(memory_db))
            conn.execute(
                """
                CREATE TABLE items (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    summary TEXT,
                    preview_text TEXT,
                    extracted_text TEXT,
                    keywords TEXT,
                    is_excluded INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO items (id, title, summary, preview_text, extracted_text, keywords, is_excluded)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    "item-1",
                    "Swarm Alpha kickoff",
                    "Fundraising notes",
                    "Athair planning",
                    "Swarm Alpha planning and investor outreach",
                    "swarm,alpha,fundraising",
                ),
            )
            conn.execute(
                """
                INSERT INTO items (id, title, summary, preview_text, extracted_text, keywords, is_excluded)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    "item-2",
                    "Athair investor mail",
                    "Follow up with investor",
                    "Swarm context",
                    "Athair investor follow up with Swarm Alpha references",
                    "athair,investor,swarm",
                ),
            )
            conn.commit()
            conn.close()

            result = clean_entities.rebuild_network(
                memory_db=str(memory_db),
                cognitive_db=str(cognitive_db),
                entity_limit_per_item=8,
            )

            self.assertGreater(result["new_nodes"], 0)
            self.assertGreater(result["new_edges"], 0)


if __name__ == "__main__":
    unittest.main()
