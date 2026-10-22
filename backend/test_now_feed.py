"""
Unit tests for the predictive "Now" feed.

These tests seed a fresh SQLite DB with crafted items + interaction_events
rows, then assert that build_now_feed returns them in the right order for
specific time-of-day / recency scenarios. They avoid FAISS and the embedder
entirely — the feed is metadata-only by design.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Stub sentence_transformers before importing backend.* — the feed doesn't
# need it, but some transitive imports will pull it in.
if "sentence_transformers" not in sys.modules:
    stub = types.ModuleType("sentence_transformers")
    class _Stub:  # pragma: no cover
        def __init__(self, *a, **kw): ...
        def encode(self, *a, **kw): return []
    stub.SentenceTransformer = _Stub  # type: ignore[attr-defined]
    sys.modules["sentence_transformers"] = stub


TMPDIR = Path(tempfile.mkdtemp())
TEST_DB_PATH = TMPDIR / "now_feed_test.db"

os.environ["RECURDO_DATA_DIR"] = str(TMPDIR / "recurdo_data")
os.environ["RECURDO_ENV_FILE"] = str(TMPDIR / "test.env")
os.environ["DB_PATH"] = str(TEST_DB_PATH)
os.environ["TEXT_FAISS_PATH"] = str(TMPDIR / "text_faiss.index")
os.environ["WATCHED_DIRS"] = str(TMPDIR)
os.environ["GEMINI_API_KEY"] = ""


from backend import database, interactions, now_feed  # noqa: E402


def _fresh_db() -> None:
    TMPDIR.mkdir(parents=True, exist_ok=True)
    (TMPDIR / "recurdo_data").mkdir(parents=True, exist_ok=True)
    database.DB_PATH = TEST_DB_PATH
    database.config.DB_PATH = TEST_DB_PATH
    TEST_DB_PATH.unlink(missing_ok=True)
    database.init_db()


def _insert_item(
    conn,
    *,
    item_id: str,
    title: str,
    source_type: str = "markdown",
    modified_at: str | None = None,
    first_seen_at: str | None = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO items (
            id, content_hash, title, filename, file_type, handler_type,
            source_type, original_path, created_at, modified_at, first_seen_at,
            file_mtime, file_size_bytes, importance_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            f"hash-{item_id}",
            title,
            f"{item_id}.md",
            "md",
            "markdown",
            source_type,
            f"/tmp/{item_id}.md",
            now_iso,
            modified_at,
            first_seen_at or now_iso,
            0.0,
            1024,
            0.5,
        ),
    )
    conn.commit()


def _log_event(
    conn,
    *,
    item_id: str,
    event_type: str,
    when: datetime,
    dwell_ms: int | None = None,
) -> None:
    event = interactions.InteractionEventIn(
        event_type=event_type,
        item_id=item_id,
        dwell_ms=dwell_ms,
        client_timestamp=when.isoformat(),
    )
    # insert_interaction_events uses its own 'now' for created_at; we need the
    # historical created_at value because the hour-of-day signal reads it
    # directly. Monkey-patch the row by updating after insert.
    interactions.insert_interaction_events(conn, [event])
    conn.execute(
        """
        UPDATE interaction_events
        SET created_at = ?
        WHERE rowid = (
            SELECT rowid FROM interaction_events
            WHERE item_id = ? AND event_type = ?
            ORDER BY rowid DESC LIMIT 1
        )
        """,
        (when.isoformat(), item_id, event_type),
    )
    conn.commit()


class InteractionRecencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _fresh_db()

    def setUp(self) -> None:
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.execute("DELETE FROM items")
            conn.commit()
        finally:
            conn.close()

    def test_recently_interacted_item_ranks_first(self) -> None:
        """An item touched 1h ago should beat one touched 10 days ago."""
        now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            _insert_item(conn, item_id="fresh", title="Fresh Notes")
            _insert_item(conn, item_id="stale", title="Stale Notes")
            _log_event(conn, item_id="fresh", event_type="click", when=now - timedelta(hours=1))
            _log_event(conn, item_id="stale", event_type="click", when=now - timedelta(days=10))

            feed = now_feed.build_now_feed(conn, now=now, limit=10)
        finally:
            conn.close()

        ids = [row["item_id"] for row in feed]
        self.assertEqual(ids[0], "fresh", f"expected fresh first, got {ids}")
        self.assertIn("stale", ids)

    def test_hour_of_day_affinity_lifts_habitual_item(self) -> None:
        """An item opened every day at 9am should beat one opened at 9am
        ONCE three days ago, when queried at 9am today."""
        now = datetime(2026, 4, 18, 9, 5, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            _insert_item(conn, item_id="morning", title="Morning Ritual")
            _insert_item(conn, item_id="random",  title="Random Doc")

            # 'morning' gets touched at 9am on each of the last 5 weekdays.
            for days_ago in (1, 2, 3, 4, 5):
                when = now - timedelta(days=days_ago)
                when_9am = when.replace(hour=9, minute=10)
                _log_event(conn, item_id="morning", event_type="open", when=when_9am)

            # 'random' got one click 3 days ago at 9am — similar recency band
            # as the morning item's oldest touch, but no habit.
            _log_event(
                conn,
                item_id="random",
                event_type="click",
                when=now - timedelta(days=3, hours=-0.1),  # ~3 days ago, near 9am
            )

            feed = now_feed.build_now_feed(conn, now=now, limit=10)
        finally:
            conn.close()

        ids = [row["item_id"] for row in feed]
        self.assertEqual(ids[0], "morning", f"habit should win at 9am, got {ids}")
        # The hour affinity component should be strong for the morning item.
        morning = next(r for r in feed if r["item_id"] == "morning")
        self.assertGreater(morning["components"]["hour_affinity"], 0.6)


class ModificationFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _fresh_db()

    def setUp(self) -> None:
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.execute("DELETE FROM items")
            conn.commit()
        finally:
            conn.close()

    def test_recently_modified_item_appears_without_interactions(self) -> None:
        """A brand-new file (no interaction history) should still show up
        on the strength of modification freshness alone."""
        now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            _insert_item(
                conn,
                item_id="brand-new",
                title="Just Drafted",
                modified_at=(now - timedelta(hours=2)).isoformat(),
            )
            feed = now_feed.build_now_feed(conn, now=now, limit=10)
        finally:
            conn.close()

        ids = [row["item_id"] for row in feed]
        self.assertIn("brand-new", ids)
        row = next(r for r in feed if r["item_id"] == "brand-new")
        self.assertGreater(row["components"]["modified"], 0.7)

    def test_interaction_beats_pure_freshness(self) -> None:
        """Signal weights: interaction should dominate freshness when both
        items were touched in the last 24h. Guards against regressions where
        a passive file-system change outranks 'you opened this yesterday'."""
        now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            _insert_item(conn, item_id="worked",   title="In Progress",
                         modified_at=(now - timedelta(days=7)).isoformat())
            _insert_item(conn, item_id="touched",  title="Just Saved",
                         modified_at=(now - timedelta(minutes=30)).isoformat())
            _log_event(conn, item_id="worked", event_type="open",
                       when=now - timedelta(hours=3))

            feed = now_feed.build_now_feed(conn, now=now, limit=10)
        finally:
            conn.close()

        ids = [row["item_id"] for row in feed]
        self.assertEqual(ids[0], "worked", f"interaction should beat pure freshness, got {ids}")


class DiversificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _fresh_db()

    def setUp(self) -> None:
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.execute("DELETE FROM items")
            conn.commit()
        finally:
            conn.close()

    def test_source_type_diversification_applied(self) -> None:
        """If you've been drowning in Gmail, the feed should still surface at
        least one non-Gmail item when others exist with reasonable scores."""
        now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            for i in range(5):
                item_id = f"gmail-{i}"
                _insert_item(conn, item_id=item_id, title=f"Thread {i}",
                             source_type="gmail")
                _log_event(conn, item_id=item_id, event_type="click",
                           when=now - timedelta(hours=1 + i * 0.1))

            _insert_item(conn, item_id="doc", title="Important Memo",
                         source_type="markdown")
            _log_event(conn, item_id="doc", event_type="click",
                       when=now - timedelta(hours=3))

            feed = now_feed.build_now_feed(conn, now=now, limit=6)
        finally:
            conn.close()

        source_types = [row["source_type"] for row in feed]
        # The gmail items are all fresher than the doc, so un-diversified the
        # doc would land somewhere near position 6. With diversification, it
        # should climb noticeably.
        doc_position = next(
            (i for i, r in enumerate(feed) if r["item_id"] == "doc"), -1
        )
        self.assertGreaterEqual(doc_position, 0, f"doc missing from feed: {feed}")
        self.assertLess(
            doc_position, 4,
            f"doc should rise via diversification, was at position {doc_position}",
        )
        self.assertIn("markdown", source_types)


class ResumeSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _fresh_db()

    def setUp(self) -> None:
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.execute("DELETE FROM items")
            conn.commit()
        finally:
            conn.close()

    def test_copy_signal_triggers_resume_boost(self) -> None:
        """Two items with identical click recency: the one where the user
        also *copied* content should outrank the plain-click one."""
        now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            _insert_item(conn, item_id="copied",  title="Pulled a quote from this")
            _insert_item(conn, item_id="clicked", title="Just looked at this")
            _log_event(conn, item_id="copied",  event_type="click", when=now - timedelta(hours=2))
            _log_event(conn, item_id="copied",  event_type="copy",  when=now - timedelta(hours=2))
            _log_event(conn, item_id="clicked", event_type="click", when=now - timedelta(hours=2))

            feed = now_feed.build_now_feed(conn, now=now, limit=10)
        finally:
            conn.close()

        ids = [row["item_id"] for row in feed]
        self.assertEqual(ids[0], "copied", f"expected copy-boosted item first: {ids}")


class FeedShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _fresh_db()

    def test_empty_library_returns_empty_list(self) -> None:
        now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.execute("DELETE FROM items")
            conn.commit()
            feed = now_feed.build_now_feed(conn, now=now, limit=10)
        finally:
            conn.close()
        self.assertEqual(feed, [])

    def test_output_shape_matches_contract(self) -> None:
        """The frontend contract: each row has the keys it renders from."""
        now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
        conn = database.get_connection()
        try:
            conn.execute("DELETE FROM interaction_events")
            conn.execute("DELETE FROM items")
            conn.commit()
            _insert_item(conn, item_id="x", title="Shape Test",
                         modified_at=(now - timedelta(hours=1)).isoformat())
            feed = now_feed.build_now_feed(conn, now=now, limit=5)
        finally:
            conn.close()

        self.assertEqual(len(feed), 1)
        row = feed[0]
        required_keys = {
            "item_id", "title", "filename", "source_type",
            "modified_at", "score", "components", "reason",
        }
        self.assertTrue(required_keys.issubset(row.keys()),
                        f"missing keys: {required_keys - row.keys()}")
        self.assertIsInstance(row["components"], dict)
        self.assertGreater(row["score"], 0.0)
        self.assertTrue(row["reason"], "reason should be a non-empty string")


import atexit  # noqa: E402

@atexit.register
def _final_cleanup() -> None:
    shutil.rmtree(TMPDIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
