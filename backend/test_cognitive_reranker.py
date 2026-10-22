from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from backend.cognition.reranker import rerank_results
from backend.cognition.store import CognitiveStore
from backend.graph.store import GraphStore
from backend.retrieval.search import SearchResult


class CognitiveRerankerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = CognitiveStore(Path(self.temp_dir.name) / "cognitive_layer.db")
        self.graph_store = GraphStore(Path(self.temp_dir.name) / "graph.db")

    def test_more_active_item_is_promoted(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_activation_event(
            "item-active",
            "open",
            occurred_at=now - timedelta(minutes=5),
        )
        self.store.record_activation_event(
            "item-stale",
            "open",
            occurred_at=now - timedelta(days=90),
        )

        results = [
            SearchResult(item_id="item-stale", item={"filename": "stale.txt"}, final_score=0.70),
            SearchResult(item_id="item-active", item={"filename": "active.txt"}, final_score=0.65),
        ]

        reranked = rerank_results(
            results,
            "project notes",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )
        self.assertEqual(reranked[0].item_id, "item-active")
        self.assertGreater(reranked[0].final_score, reranked[1].final_score)

    def test_spreading_and_frame_match_contribute(self) -> None:
        self.store.upsert_entity_edge("entity:investor", "item:item-deck", weight=1.0, edge_type="entity_item")
        self.store.upsert_entity_edge("item:item-deck", "entity:investor", weight=1.0, edge_type="entity_item")
        self.store.upsert_frame_instance(
            "item-deck",
            "pitch_deck",
            {"title": "Seed deck"},
            confidence=0.9,
        )

        results = [
            SearchResult(item_id="item-note", item={"filename": "note.txt"}, final_score=0.66),
            SearchResult(item_id="item-deck", item={"filename": "deck.pptx"}, final_score=0.60),
        ]

        reranked = rerank_results(
            results,
            "investor pitch deck",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )
        self.assertEqual(reranked[0].item_id, "item-deck")
        self.assertGreater(reranked[0].cognitive_breakdown["spreading"], 0.0)
        self.assertGreater(reranked[0].cognitive_breakdown["frame_match"], 0.0)

    def test_script_relevance_contributes_for_active_workflow(self) -> None:
        self.store.upsert_frame_instance(
            "item-email",
            "email",
            {"subject": "warm investor intro"},
            confidence=0.94,
        )
        self.store.upsert_frame_instance(
            "item-invoice",
            "invoice",
            {"status": "unpaid"},
            confidence=0.9,
        )
        self.store.upsert_frame_instance(
            "item-deck",
            "pitch_deck",
            {"title": "Seed deck"},
            confidence=0.9,
        )
        self.store.record_activation_event(
            "item-email",
            "open",
            occurred_at=datetime(2026, 3, 18, 10, 0, tzinfo=timezone.utc),
        )
        self.store.record_activation_event(
            "item-deck",
            "open",
            occurred_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
        )

        results = [
            SearchResult(item_id="item-invoice", item={"title": "Invoice April"}, final_score=0.68),
            SearchResult(
                item_id="item-email",
                item={"title": "Warm investor intro email", "summary": "email to investor"},
                final_score=0.66,
            ),
        ]

        reranked = rerank_results(
            results,
            "warm investor intro",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )
        self.assertEqual(reranked[0].item_id, "item-email")
        self.assertGreater(reranked[0].cognitive_breakdown["script_relevance"], 0.0)
        self.assertGreater(
            reranked[0].cognitive_breakdown["script_relevance"],
            reranked[1].cognitive_breakdown["script_relevance"],
        )

    def test_graph_relevance_promotes_direct_entity_match(self) -> None:
        investor_id = self.graph_store.upsert_entity(name="Investor", entity_type="Person")
        roadmap_id = self.graph_store.upsert_entity(name="Roadmap", entity_type="Concept")
        note_id = self.graph_store.upsert_entity(name="Engineering", entity_type="Concept")

        self.graph_store.link_episode_to_entity("item-deck", investor_id)
        self.graph_store.link_episode_to_entity("item-deck", roadmap_id)
        self.graph_store.link_episode_to_entity("item-note", note_id)

        results = [
            SearchResult(item_id="item-note", item={"filename": "notes.txt"}, final_score=0.69),
            SearchResult(item_id="item-deck", item={"filename": "deck.pptx"}, final_score=0.65),
        ]

        reranked = rerank_results(
            results,
            "investor update",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )
        self.assertEqual(reranked[0].item_id, "item-deck")
        self.assertGreater(reranked[0].cognitive_breakdown["graph_relevance"], 0.0)
        self.assertIn("Investor", reranked[0].cognitive_breakdown["graph_direct_matches"])

    def test_graph_relevance_uses_expanded_stock_query_entities(self) -> None:
        finance_id = self.graph_store.upsert_entity(name="Finance", entity_type="Concept")
        lecture_id = self.graph_store.upsert_entity(name="Lecture", entity_type="Concept")

        self.graph_store.link_episode_to_entity("item-athair", finance_id)
        self.graph_store.link_episode_to_entity("item-lecture", lecture_id)

        results = [
            SearchResult(item_id="item-lecture", item={"filename": "linear regression.pptx"}, final_score=0.70),
            SearchResult(item_id="item-athair", item={"filename": "athair sample.pdf"}, final_score=0.65),
        ]

        reranked = rerank_results(
            results,
            "the stocks related project i was working on in february",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )
        self.assertEqual(reranked[0].item_id, "item-athair")
        self.assertIn("entity:finance", reranked[0].cognitive_breakdown["query_entities"])
        self.assertGreater(reranked[0].cognitive_breakdown["graph_relevance"], 0.0)

    def test_generic_queries_keep_gmail_attachments_below_local_files(self) -> None:
        results = [
            SearchResult(
                item_id="item-gmail-attachment",
                item={
                    "filename": "recurdo_unified_master.md",
                    "source_type": "gmail_attachment",
                },
                final_score=0.82,
            ),
            SearchResult(
                item_id="item-local",
                item={
                    "filename": "Recurdo_BIBLE.pdf",
                    "source_type": "local",
                },
                final_score=0.62,
            ),
        ]

        reranked = rerank_results(
            results,
            "what was i working on in february for recurdo",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )

        self.assertEqual(reranked[0].item_id, "item-local")
        self.assertLess(reranked[1].cognitive_breakdown["source_type_guardrail"], 1.0)

    def test_explicit_email_queries_keep_gmail_attachments_available(self) -> None:
        results = [
            SearchResult(
                item_id="item-local",
                item={"filename": "seed_deck_notes.md", "source_type": "local"},
                final_score=0.62,
            ),
            SearchResult(
                item_id="item-gmail-attachment",
                item={"filename": "seed_deck.pdf", "source_type": "gmail_attachment"},
                final_score=0.82,
            ),
        ]

        reranked = rerank_results(
            results,
            "email seed deck attachment",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )

        self.assertEqual(reranked[0].item_id, "item-gmail-attachment")
        self.assertEqual(reranked[0].cognitive_breakdown["source_type_guardrail"], 1.0)

    def test_project_recall_keeps_substantive_doc_above_roadmap_meta_doc(self) -> None:
        results = [
            SearchResult(
                item_id="item-roadmap",
                item={
                    "title": "RECURDO — FROM HERE TO REAL USERS",
                    "filename": "recurdo_from_here_to_users.md",
                    "source_type": "local",
                },
                final_score=0.82,
            ),
            SearchResult(
                item_id="item-bible",
                item={
                    "title": "Recurdo BIBLE",
                    "filename": "Recurdo_BIBLE.pdf",
                    "source_type": "local",
                },
                final_score=0.62,
            ),
        ]

        reranked = rerank_results(
            results,
            "what was i working on in february for recurdo",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )

        self.assertEqual(reranked[0].item_id, "item-bible")
        self.assertLess(reranked[1].cognitive_breakdown["project_meta_guardrail"], 1.0)

    def test_explicit_roadmap_query_keeps_roadmap_meta_doc(self) -> None:
        results = [
            SearchResult(
                item_id="item-roadmap",
                item={
                    "title": "RECURDO — FROM HERE TO REAL USERS",
                    "filename": "recurdo_from_here_to_users.md",
                    "source_type": "local",
                },
                final_score=0.82,
            ),
            SearchResult(
                item_id="item-bible",
                item={
                    "title": "Recurdo BIBLE",
                    "filename": "Recurdo_BIBLE.pdf",
                    "source_type": "local",
                },
                final_score=0.62,
            ),
        ]

        reranked = rerank_results(
            results,
            "recurdo from here to users",
            cognitive_store=self.store,
            graph_store=self.graph_store,
        )

        self.assertEqual(reranked[0].item_id, "item-roadmap")
        self.assertEqual(reranked[0].cognitive_breakdown["project_meta_guardrail"], 1.0)


if __name__ == "__main__":
    unittest.main()
