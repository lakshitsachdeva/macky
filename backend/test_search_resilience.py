from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


TMPDIR = Path(tempfile.mkdtemp())
TEST_DB_PATH = TMPDIR / "memory.db"

os.environ["RECURDO_DATA_DIR"] = str(TMPDIR / "recurdo_data")
os.environ["RECURDO_ENV_FILE"] = str(TMPDIR / "test.env")
os.environ["DB_PATH"] = str(TEST_DB_PATH)
os.environ["TEXT_FAISS_PATH"] = str(TMPDIR / "text_faiss.index")
os.environ["WATCHED_DIRS"] = str(TMPDIR)
os.environ["GEMINI_API_KEY"] = ""

from backend import database
from backend.retrieval import search

database.DB_PATH = TEST_DB_PATH
database.config.DB_PATH = TEST_DB_PATH


def _insert_searchable_item() -> None:
    conn = database.get_connection()
    try:
        database.insert_item(
            conn,
            {
                "id": "iit-image-1",
                "content_hash": "iit-image-hash-1",
                "title": "IIT Madras notice",
                "filename": "iit_madras_notice.png",
                "file_type": "image",
                "handler_type": "visual",
                "source_type": "local",
                "original_path": str(TMPDIR / "iit_madras_notice.png"),
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
                "extracted_text": (
                    "Indian Institute of Technology Madras Summer Fellowship "
                    "Programme 2026 Apply at ssp.iitm.ac.in"
                ),
                "preview_text": "Indian Institute of Technology Madras",
                "summary": "IIT Madras fellowship poster",
                "keywords": '["iit","madras","fellowship"]',
            },
        )
        database.insert_item(
            conn,
            {
                "id": "marksheet-image-1",
                "content_hash": "marksheet-image-hash-1",
                "title": "e47e39ea-e452-454f-9cfc-615d8fdb0ad4",
                "filename": "e47e39ea-e452-454f-9cfc-615d8fdb0ad4.jpeg",
                "file_type": "image",
                "handler_type": "visual",
                "source_type": "local",
                "original_path": str(TMPDIR / "e47e39ea-e452-454f-9cfc-615d8fdb0ad4.jpeg"),
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
                "extracted_text": (
                    "NMIMS University Result Semester IV Final Exam "
                    "Complex Variables Database Management Systems"
                ),
                "preview_text": "NMIMS University result",
                "summary": "Semester result screenshot",
                "keywords": '["nmims","result","semester","marksheet"]',
            },
        )
        database.insert_item(
            conn,
            {
                "id": "security-presentation-1",
                "content_hash": "security-presentation-hash-1",
                "title": "Network Security Presentation",
                "filename": "network_security_reference.pdf",
                "file_type": "pdf",
                "handler_type": "text",
                "source_type": "local",
                "original_path": str(TMPDIR / "network_security_reference.pdf"),
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
                "extracted_text": "",
                "preview_text": "Network security presentation reference",
                "summary": "",
                "keywords": "[]",
            },
        )
        created_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO embeddings (item_id, embedding_type, faiss_id, sparse_weights, model_name, created_at)
            VALUES (?, 'dense', ?, NULL, 'test-model', ?)
            """,
            ("iit-image-1", 1, created_at),
        )
        conn.execute(
            """
            INSERT INTO embeddings (item_id, embedding_type, faiss_id, sparse_weights, model_name, created_at)
            VALUES (?, 'dense', ?, NULL, 'test-model', ?)
            """,
            ("marksheet-image-1", 2, created_at),
        )
        conn.commit()
    finally:
        conn.close()


class SearchResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database.DB_PATH = TEST_DB_PATH
        database.config.DB_PATH = TEST_DB_PATH
        TEST_DB_PATH.unlink(missing_ok=True)
        database.init_db()
        _insert_searchable_item()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def test_search_returns_results_when_dense_embedding_times_out(self) -> None:
        async def slow_embed_query(_: str):
            await asyncio.sleep(0.05)
            return None

        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": True},
        ), mock.patch.object(
            search.embedder,
            "embed_query_async",
            side_effect=slow_embed_query,
        ), mock.patch.object(
            search.embedder,
            "ensure_model_pool_warmup_started",
        ) as warmup_mock, mock.patch.object(
            search,
            "QUERY_EMBED_TIMEOUT_SECONDS",
            0.01,
        ):
            results = asyncio.run(search.search("iit madras", limit=5))

        self.assertTrue(results)
        self.assertEqual(results[0].item["filename"], "iit_madras_notice.png")
        warmup_mock.assert_called_once()

    def test_search_skips_dense_when_query_pool_is_cold(self) -> None:
        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": False},
        ), mock.patch.object(
            search.embedder,
            "embed_query_async",
            side_effect=AssertionError("dense embedding should not run while pool is cold"),
        ), mock.patch.object(
            search.embedder,
            "ensure_model_pool_warmup_started",
        ) as warmup_mock:
            results = asyncio.run(search.search("iit madras", limit=5))

        self.assertTrue(results)
        self.assertEqual(results[0].item["filename"], "iit_madras_notice.png")
        warmup_mock.assert_called_once()

    def test_non_temporal_query_does_not_inject_unrelated_recent_items(self) -> None:
        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": False},
        ), mock.patch.object(
            search.embedder,
            "embed_query_async",
            side_effect=AssertionError("dense embedding should not run while pool is cold"),
        ), mock.patch.object(
            search.embedder,
            "ensure_model_pool_warmup_started",
        ):
            results = asyncio.run(search.search("volcanic zebras", limit=5))

        self.assertEqual(results, [])

    def test_identifier_fts_query_requires_all_marker_tokens(self) -> None:
        queries = search._fts_token_query(
            "zzzzzz-no-such-term-123",
            require_all=True,
        )

        self.assertEqual(queries, ["zzzzzz no such term 123"])

    def test_identifier_query_does_not_return_dense_only_neighbours(self) -> None:
        class FakeIndex:
            def search(self, _dense_vec, k=40):
                return [1][:k], [0.95][:k]

        async def fake_embed_query(_: str):
            return object(), None

        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": True},
        ), mock.patch.object(
            search.embedder,
            "embed_query_async",
            side_effect=fake_embed_query,
        ), mock.patch.object(
            search.embedder,
            "get_text_index",
            return_value=FakeIndex(),
        ):
            results = asyncio.run(search.search("zzzzzz-no-such-term-123", limit=5))

        self.assertEqual(results, [])

    def test_strong_title_match_survives_natural_language_file_type_hint(self) -> None:
        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": False},
        ), mock.patch.object(
            search.embedder,
            "ensure_model_pool_warmup_started",
        ), mock.patch(
            "backend.cognition.reranker.rerank_results",
            side_effect=lambda results, query: results,
        ):
            results = asyncio.run(
                search.search("find the network security presentation", limit=5)
            )

        self.assertTrue(results)
        self.assertEqual(results[0].item["id"], "security-presentation-1")

    def test_fast_pipeline_skips_cognitive_rerank(self) -> None:
        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": False},
        ), mock.patch.object(
            search.embedder,
            "embed_query_async",
            side_effect=AssertionError("dense embedding should not run while pool is cold"),
        ), mock.patch.object(
            search.embedder,
            "ensure_model_pool_warmup_started",
        ), mock.patch(
            "backend.cognition.reranker.rerank_results",
            side_effect=AssertionError("fast pipeline should skip cognitive rerank"),
        ):
            results, debug = asyncio.run(search.search("iit madras", limit=5, return_debug=True))

        self.assertTrue(results)
        self.assertEqual(debug["pipeline"], "fast")
        self.assertEqual(debug["latency_ms"]["cognitive_rerank"], 0.0)

    def test_visual_query_prefers_lexically_anchored_image_over_dense_only_lookalike(self) -> None:
        class FakeIndex:
            def search(self, _dense_vec, k=40):
                return [2, 1][:k], [0.55, 0.52][:k]

        async def fake_embed_query(_: str):
            return object(), None

        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": True},
        ), mock.patch.object(
            search.embedder,
            "embed_query_async",
            side_effect=fake_embed_query,
        ), mock.patch.object(
            search.embedder,
            "get_text_index",
            return_value=FakeIndex(),
        ), mock.patch.object(
            search.embedder,
            "compute_sparse_score",
            return_value=0.0,
        ), mock.patch(
            "backend.cognition.reranker.rerank_results",
            side_effect=lambda results, query: results,
        ):
            results = asyncio.run(
                search.search("that image that i had about that iit madras internshop", limit=5)
            )

        self.assertGreaterEqual(len(results), 2)
        self.assertEqual(results[0].item["id"], "iit-image-1")
        self.assertEqual(results[1].item["id"], "marksheet-image-1")
        self.assertGreater(results[0].final_score, results[1].final_score)


if __name__ == "__main__":
    unittest.main()
