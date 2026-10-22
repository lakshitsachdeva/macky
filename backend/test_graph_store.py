from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from backend.graph.store import GraphStore


class GraphStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.graph_db = Path(self.temp_dir.name) / "graph.db"
        self.store = GraphStore(self.graph_db)

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
        self.assertIn("rahul@acme.com", entity["aliases"])

    def test_upsert_entity_merges_canonical_key_conflicts(self) -> None:
        alias_owner_id = str(uuid.uuid4())
        canonical_owner_id = str(uuid.uuid4())
        with self.store.get_connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO entities (
                    id,
                    canonical_key,
                    name,
                    entity_type,
                    aliases,
                    attributes,
                    mention_count,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    alias_owner_id,
                    "ai",
                    "AI",
                    "Concept",
                    json.dumps(["AI", "AI Engineering"], ensure_ascii=True),
                    "{}",
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO entities (
                    id,
                    canonical_key,
                    name,
                    entity_type,
                    aliases,
                    attributes,
                    mention_count,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    canonical_owner_id,
                    "ai engineering",
                    "AI Engineering",
                    "Concept",
                    json.dumps(["AI Engineering"], ensure_ascii=True),
                    "{}",
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute(
                "INSERT INTO entity_aliases (normalized_alias, entity_id, alias) VALUES (?, ?, ?)",
                ("ai", alias_owner_id, "AI"),
            )
            conn.execute(
                "INSERT INTO entity_aliases (normalized_alias, entity_id, alias) VALUES (?, ?, ?)",
                ("ai engineering", alias_owner_id, "AI Engineering"),
            )

        merged_id = self.store.upsert_entity(
            name="AI Engineering",
            entity_type="Concept",
            aliases=["AI"],
        )

        self.assertEqual(merged_id, canonical_owner_id)
        self.assertNotEqual(alias_owner_id, canonical_owner_id)
        self.assertEqual(self.store.get_counts()["entities"], 1)

        entity = self.store.get_entity_by_name("AI Engineering")
        self.assertIsNotNone(entity)
        assert entity is not None
        self.assertEqual(entity["name"], "AI Engineering")
        self.assertIn("AI", entity["aliases"])

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

        before_change = self.store.get_facts_at_time(project_id, datetime(2026, 4, 2, tzinfo=timezone.utc))
        after_change = self.store.get_facts_at_time(project_id, datetime(2026, 4, 6, tzinfo=timezone.utc))
        self.assertEqual(before_change[0]["object"], "draft")
        self.assertEqual(after_change[0]["object"], "submitted")

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

    def test_search_entities_supports_partial_name_matching(self) -> None:
        self.store.upsert_entity(
            name="Rahul Sharma",
            entity_type="Person",
            aliases=["rahul@acme.com"],
        )
        self.store.upsert_entity(
            name="Recurdo Fundraising",
            entity_type="Project",
        )

        matches = self.store.search_entities("rahul", limit=5)
        self.assertGreaterEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "Rahul Sharma")

        project_matches = self.store.search_entities("fundraising", limit=5)
        self.assertGreaterEqual(len(project_matches), 1)
        self.assertEqual(project_matches[0]["name"], "Recurdo Fundraising")

    def test_detect_communities_groups_related_entities(self) -> None:
        fundraising = self.store.upsert_entity(name="Fundraising", entity_type="Concept")
        investor = self.store.upsert_entity(name="Investor", entity_type="Person")
        term_sheet = self.store.upsert_entity(name="Term Sheet", entity_type="Document")
        invoice = self.store.upsert_entity(name="Invoice", entity_type="Document")
        client = self.store.upsert_entity(name="Client", entity_type="Organisation")
        payment = self.store.upsert_entity(name="Payment", entity_type="Concept")

        for episode_id, entity_ids in {
            "episode-1": [fundraising, investor, term_sheet],
            "episode-2": [fundraising, investor, term_sheet],
            "episode-3": [invoice, client, payment],
            "episode-4": [invoice, client, payment],
        }.items():
            for entity_id in entity_ids:
                self.store.link_episode_to_entity(episode_id, entity_id)

        communities = self.store.detect_communities(limit=10, min_size=3, min_shared_mentions=2)
        labels = [community["label"] for community in communities]

        self.assertGreaterEqual(len(communities), 2)
        self.assertTrue(any("Fundraising" in label for label in labels))
        self.assertTrue(any("Invoice" in label or "Payment" in label for label in labels))

    def test_weak_signal_promotion_and_expiry(self) -> None:
        self.store.upsert_entity(name="Rahul Sharma", entity_type="Person")
        self.store.upsert_entity(name="Lakshit Sachdeva", entity_type="Person")
        for episode_id in ("episode-1", "episode-2", "episode-3"):
            count, should_promote = self.store.record_weak_signal(
                "Rahul Sharma",
                "Lakshit Sachdeva",
                "CO_AUTHORED",
                episode_id,
                0.45,
            )
        self.assertEqual(count, 3)
        self.assertTrue(should_promote)

        promoted = self.store.run_promotions()
        self.assertEqual(len(promoted), 1)

        timeline = self.store.get_entity_timeline("Rahul Sharma")
        self.assertTrue(
            any(
                entry["predicate"] == "COLLABORATES_WITH"
                and {entry["subject"], entry["object"]} == {"Rahul Sharma", "Lakshit Sachdeva"}
                for entry in timeline
            )
        )

        self.store.record_weak_signal(
            "Old Person",
            "Old Org",
            "REPRESENTS_ORG",
            "episode-old",
            0.4,
        )
        with self.store.get_connection() as conn:
            conn.execute(
                "UPDATE weak_signals SET last_seen = ? WHERE entity_a = ? AND entity_b = ?",
                (0.0, "Old Org", "Old Person"),
            )
        expired = self.store.expire_old_signals(older_than_days=1)
        self.assertEqual(expired, 1)

    def test_collaboration_promotion_requires_credible_person_entities(self) -> None:
        self.store.upsert_entity(name="Graph", entity_type="Person")
        self.store.upsert_entity(name="Top", entity_type="Person")

        for episode_id in ("episode-1", "episode-2", "episode-3"):
            count, should_promote = self.store.record_weak_signal(
                "Graph",
                "Top",
                "CO_AUTHORED",
                episode_id,
                0.8,
            )

        self.assertEqual(count, 3)
        self.assertTrue(should_promote)
        promoted = self.store.run_promotions()
        self.assertEqual(promoted, [])

        timeline = self.store.get_entity_timeline("Graph")
        self.assertFalse(any(entry["predicate"] == "COLLABORATES_WITH" for entry in timeline))

    def test_delete_low_confidence_collaborations_removes_non_person_like_pairs(self) -> None:
        valid_left = self.store.upsert_entity(name="Rahul Sharma", entity_type="Person")
        valid_right = self.store.upsert_entity(name="Lakshit Sachdeva", entity_type="Person")
        noisy_left = self.store.upsert_entity(name="Graph", entity_type="Person")
        noisy_right = self.store.upsert_entity(name="Top", entity_type="Person")

        self.store.add_temporal_fact(
            subject_id=valid_left,
            predicate="COLLABORATES_WITH",
            object_id=valid_right,
            valid_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
            episode_id="valid-1",
            confidence=0.8,
        )
        self.store.add_temporal_fact(
            subject_id=noisy_left,
            predicate="COLLABORATES_WITH",
            object_id=noisy_right,
            valid_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
            episode_id="noisy-1",
            confidence=0.8,
        )

        summary = self.store.delete_low_confidence_collaborations()

        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["reason_counts"]["non_person_like"], 1)

        timeline = self.store.get_entity_timeline("Rahul Sharma")
        self.assertTrue(any(entry["predicate"] == "COLLABORATES_WITH" for entry in timeline))
        noisy_timeline = self.store.get_entity_timeline("Graph")
        self.assertFalse(any(entry["predicate"] == "COLLABORATES_WITH" for entry in noisy_timeline))

    def test_get_facts_at_time_includes_reverse_relationships(self) -> None:
        rahul_id = self.store.upsert_entity(name="Rahul Sharma", entity_type="Person")
        lakshit_id = self.store.upsert_entity(name="Lakshit Sachdeva", entity_type="Person")
        self.store.add_temporal_fact(
            subject_id=rahul_id,
            predicate="SENT_TO",
            object_id=lakshit_id,
            valid_from=datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc),
            episode_id="email-1",
            confidence=1.0,
        )

        facts = self.store.get_facts_at_time(lakshit_id, datetime(2026, 4, 7, tzinfo=timezone.utc))

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["entity_role"], "object")
        self.assertEqual(facts[0]["subject"], "Rahul Sharma")
        self.assertEqual(facts[0]["object"], "Lakshit Sachdeva")


if __name__ == "__main__":
    unittest.main()
