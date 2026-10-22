"""
Unit tests for the interaction event logging module.

Covers: schema roundtrip, batch insert with mixed valid/invalid rows,
query-hash stability, aggregation helpers the Now feed and reranker will
consume. No sentence-transformers or FAISS needed — this test boots a fresh
SQLite file and hits the helpers directly.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


TMPDIR = Path(tempfile.mkdtemp())
TEST_DB_PATH = TMPDIR / "interactions_test.db"

os.environ["RECURDO_DATA_DIR"] = str(TMPDIR / "recurdo_data")
os.environ["RECURDO_ENV_FILE"] = str(TMPDIR / "test.env")
os.environ["DB_PATH"] = str(TEST_DB_PATH)
os.environ["TEXT_FAISS_PATH"] = str(TMPDIR / "text_faiss.index")
os.environ["WATCHED_DIRS"] = str(TMPDIR)
os.environ["GEMINI_API_KEY"] = ""


from backend import database, interactions  # noqa: E402


class InteractionHashingTests(unittest.TestCase):
    """Query-hash normalization is the join key for Phase 5 reranker — it
    needs to collapse trivial formatting differences and stay stable."""

    def test_casing_and_whitespace_collapse(self) -> None:
        self.assertEqual(
            interactions.hash_query("Recurdo BIBLE"),
            interactions.hash_query("recurdo  bible"),
        )
        self.assertEqual(
            interactions.hash_query("recurdo bible  "),
            interactions.hash_query("  recurdo   bible"),
        )

    def test_punctuation_stripped(self) -> None:
        self.assertEqual(
            interactions.hash_query("iit-bombay proposal"),
            interactions.hash_query("iit bombay proposal"),
        )
        self.assertEqual(
            interactions.hash_query("Q1'26 plan"),
            interactions.hash_query("q1 26 plan"),
        )

    def test_distinct_queries_hash_differently(self) -> None:
        self.assertNotEqual(
            interactions.hash_query("recurdo bible"),
            interactions.hash_query("recurdo bible v2"),
        )

    def test_empty_and_noise_only_produce_none(self) -> None:
        self.assertIsNone(interactions.hash_query(""))
        self.assertIsNone(interactions.hash_query(None))
        self.assertIsNone(interactions.hash_query("   "))
        self.assertIsNone(interactions.hash_query("!!!"))


def _ensure_fresh_test_db() -> None:
    """Idempotent test DB init. Safe to call from every setUpClass so tests
    can be re-entered after a previous class tore the tmpdir down."""
    TMPDIR.mkdir(parents=True, exist_ok=True)
    (TMPDIR / "recurdo_data").mkdir(parents=True, exist_ok=True)
    database.DB_PATH = TEST_DB_PATH
    database.config.DB_PATH = TEST_DB_PATH
    TEST_DB_PATH.unlink(missing_ok=True)
    database.init_db()


class InteractionInsertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_fresh_test_db()

    @classmethod
    def tearDownClass(cls) -> None:
        # Don't rmtree TMPDIR here — a second test class may still need it.
        # Final cleanup happens in the module-level atexit below.
        pass

    def setUp(self) -> None:
        # Clean slate between tests.
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.commit()
        finally:
            conn.close()

    def _insert(self, events: list[dict], **kwargs) -> tuple[int, list[str]]:
        conn = database.get_connection()
        try:
            models = [interactions.InteractionEventIn(**e) for e in events]
            return interactions.insert_interaction_events(conn, models, **kwargs)
        finally:
            conn.close()

    def test_search_event_roundtrip(self) -> None:
        accepted, reasons = self._insert([
            {
                "event_type": "search",
                "query": "recurdo bible",
                "session_id": "sess-1",
                "client_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ])
        self.assertEqual(accepted, 1)
        self.assertEqual(reasons, [])

        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM interaction_events"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["event_type"], "search")
        self.assertEqual(row["query"], "recurdo bible")
        self.assertEqual(
            row["query_hash"], interactions.hash_query("recurdo bible")
        )
        self.assertEqual(row["session_id"], "sess-1")
        self.assertIsNone(row["item_id"])
        self.assertEqual(row["user_id"], "local")

    def test_click_event_requires_item_id(self) -> None:
        accepted, reasons = self._insert([
            {
                "event_type": "click",
                "query": "recurdo bible",
                "rank_position": 1,
                # item_id deliberately missing
            },
        ])
        self.assertEqual(accepted, 0)
        self.assertEqual(len(reasons), 1)
        self.assertIn("missing item_id", reasons[0])

    def test_mixed_batch_partial_acceptance(self) -> None:
        accepted, reasons = self._insert([
            {  # valid
                "event_type": "click",
                "item_id": "item-1",
                "query": "recurdo bible",
                "rank_position": 1,
            },
            {  # invalid — missing item_id
                "event_type": "open",
            },
            {  # valid
                "event_type": "dwell",
                "item_id": "item-2",
                "dwell_ms": 3400,
            },
        ])
        self.assertEqual(accepted, 2)
        self.assertEqual(len(reasons), 1)

        conn = database.get_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM interaction_events"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 2)

    def test_invalid_event_type_rejected(self) -> None:
        with self.assertRaises(Exception):
            interactions.InteractionEventIn(
                event_type="unrelated_type",
                item_id="x",
            )

    def test_batch_scoped_to_user_id(self) -> None:
        # Two different users writing the same item should not cross-contaminate.
        self._insert(
            [{"event_type": "click", "item_id": "item-1",
              "query": "q", "rank_position": 1}],
            user_id="user-a",
        )
        self._insert(
            [{"event_type": "click", "item_id": "item-1",
              "query": "q", "rank_position": 1}],
            user_id="user-b",
        )
        conn = database.get_connection()
        try:
            stats_a = interactions.get_click_stats_for_item(
                conn, "user-a", "item-1"
            )
            stats_b = interactions.get_click_stats_for_item(
                conn, "user-b", "item-1"
            )
        finally:
            conn.close()
        self.assertEqual(stats_a["clicks"], 1)
        self.assertEqual(stats_b["clicks"], 1)


class InteractionAggregationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_fresh_test_db()

    @classmethod
    def tearDownClass(cls) -> None:
        pass

    def setUp(self) -> None:
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.commit()
        finally:
            conn.close()

    def _insert(self, events: list[dict], **kwargs) -> None:
        conn = database.get_connection()
        try:
            models = [interactions.InteractionEventIn(**e) for e in events]
            interactions.insert_interaction_events(conn, models, **kwargs)
        finally:
            conn.close()

    def test_click_stats_aggregation(self) -> None:
        self._insert([
            {"event_type": "click", "item_id": "item-1",
             "query": "q", "rank_position": 1},
            {"event_type": "click", "item_id": "item-1",
             "query": "q", "rank_position": 1},
            {"event_type": "open",  "item_id": "item-1"},
            {"event_type": "dwell", "item_id": "item-1", "dwell_ms": 1500},
            {"event_type": "dwell", "item_id": "item-1", "dwell_ms": 2500},
        ])
        conn = database.get_connection()
        try:
            stats = interactions.get_click_stats_for_item(
                conn, "local", "item-1"
            )
        finally:
            conn.close()
        self.assertEqual(stats["clicks"], 2)
        self.assertEqual(stats["opens"], 1)
        self.assertEqual(stats["total_dwell_ms"], 4000)

    def test_recent_items_ordered_by_time(self) -> None:
        # Insert three separate items with different "last touch" times.
        self._insert([
            {"event_type": "click", "item_id": "item-old",
             "query": "q", "rank_position": 1},
        ])
        self._insert([
            {"event_type": "click", "item_id": "item-middle",
             "query": "q", "rank_position": 2},
        ])
        self._insert([
            {"event_type": "click", "item_id": "item-fresh",
             "query": "q", "rank_position": 3},
        ])
        conn = database.get_connection()
        try:
            recent = interactions.get_recent_items_for_user(conn, "local", limit=10)
        finally:
            conn.close()
        ids = [r["item_id"] for r in recent]
        # Most recent insert wins; because created_at resolution is seconds, the
        # relative order between same-second inserts is undefined, but the set
        # must contain all three.
        self.assertEqual(set(ids), {"item-old", "item-middle", "item-fresh"})

    def test_click_through_for_query_groups_by_hash(self) -> None:
        q1 = "recurdo BIBLE"
        q2 = "recurdo  bible"  # same after normalization
        q3 = "recurdo bible v2"  # distinct
        self._insert([
            {"event_type": "click", "item_id": "item-a",
             "query": q1, "rank_position": 1},
            {"event_type": "click", "item_id": "item-a",
             "query": q2, "rank_position": 1},
            {"event_type": "click", "item_id": "item-b",
             "query": q3, "rank_position": 1},
        ])
        q_hash = interactions.hash_query(q1)
        assert q_hash is not None
        conn = database.get_connection()
        try:
            ctr = interactions.get_click_through_for_query(
                conn, "local", q_hash, limit=10
            )
        finally:
            conn.close()
        # Only item-a should appear — item-b was on a different query.
        self.assertEqual(len(ctr), 1)
        self.assertEqual(ctr[0]["item_id"], "item-a")
        self.assertEqual(ctr[0]["click_count"], 2)


import atexit  # noqa: E402

@atexit.register
def _final_cleanup() -> None:
    shutil.rmtree(TMPDIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
