from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from backend import main


class HealthKeepaliveTests(unittest.TestCase):
    def _patch_health_dependencies(self) -> mock._patch:
        return mock.patch.multiple(
            main,
            _get_count_summary=mock.DEFAULT,
            _get_db_item_count=mock.DEFAULT,
            _get_pending_watched_file_count=mock.DEFAULT,
            _status_scan_snapshot=mock.DEFAULT,
            _get_cognitive_status_snapshot=mock.DEFAULT,
            _get_graph_status_snapshot=mock.DEFAULT,
            _get_llm_status_snapshot=mock.DEFAULT,
            _get_gmail_status_snapshot=mock.DEFAULT,
            _get_keepalive_snapshot=mock.DEFAULT,
            _keepalive_faiss_ping_sync=mock.DEFAULT,
        )

    def test_mark_query_activity_updates_timestamp(self) -> None:
        previous = main._last_query_time
        try:
            main._last_query_time = 1.0
            main._mark_query_activity()
            self.assertGreater(main._last_query_time, 1.0)
        finally:
            main._last_query_time = previous

    def test_health_payload_is_ok_with_loaded_faiss(self) -> None:
        previous_query_time = main._last_query_time
        previous_started_at = main._server_started_at
        try:
            main._last_query_time = 9_950.0
            main._server_started_at = 9_000.0
            with self._patch_health_dependencies() as patched, mock.patch.object(
                main.embedder,
                "get_model_pool_status",
                return_value={
                    "initialized": True,
                    "pool_size": 2,
                    "requested_pool_size": 2,
                    "query_pool_initialized": True,
                    "query_pool_size": 1,
                    "query_pool_requested_size": 1,
                },
            ):
                patched["_get_count_summary"].return_value = {
                    "unique_memories": 12,
                    "files_watched": 3,
                    "duplicates_skipped": 1,
                }
                patched["_get_db_item_count"].return_value = 12
                patched["_get_pending_watched_file_count"].return_value = 0
                patched["_status_scan_snapshot"].return_value = ({}, {}, False)
                patched["_get_cognitive_status_snapshot"].return_value = {
                    "ready": True,
                    "activation_events": 4,
                    "entity_edges": 8,
                    "frame_instances": 2,
                }
                patched["_get_graph_status_snapshot"].return_value = {
                    "ready": True,
                    "entities": 5,
                    "episode_mentions": 6,
                    "temporal_facts": 7,
                }
                patched["_get_llm_status_snapshot"].return_value = {"mode": "heuristic"}
                patched["_get_gmail_status_snapshot"].return_value = {"connected": False}
                patched["_get_keepalive_snapshot"].return_value = {
                    "last_success_at": 9_980.0,
                    "last_success_age_s": 20,
                    "last_failure_at": None,
                    "last_failure_age_s": None,
                    "last_error": None,
                }
                patched["_keepalive_faiss_ping_sync"].return_value = 12

                payload = main._compute_health_payload(now=10_000.0)

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["faiss_items"], 12)
            self.assertEqual(payload["db_items"], 12)
            self.assertEqual(payload["last_query_age_s"], 50)
            self.assertEqual(payload["uptime_s"], 1000)
            self.assertTrue(payload["model_loaded"])
        finally:
            main._last_query_time = previous_query_time
            main._server_started_at = previous_started_at

    def test_health_payload_is_degraded_when_faiss_probe_fails(self) -> None:
        previous_query_time = main._last_query_time
        previous_started_at = main._server_started_at
        try:
            main._last_query_time = 100.0
            main._server_started_at = 50.0
            with self._patch_health_dependencies() as patched, mock.patch.object(
                main.embedder,
                "get_model_pool_status",
                return_value={
                    "initialized": False,
                    "pool_size": 2,
                    "requested_pool_size": 2,
                    "query_pool_initialized": False,
                    "query_pool_size": 1,
                    "query_pool_requested_size": 1,
                },
            ):
                patched["_get_count_summary"].return_value = {
                    "unique_memories": 4,
                    "files_watched": 1,
                    "duplicates_skipped": 0,
                }
                patched["_get_db_item_count"].return_value = 4
                patched["_get_pending_watched_file_count"].return_value = 0
                patched["_status_scan_snapshot"].return_value = ({}, {}, False)
                patched["_get_cognitive_status_snapshot"].return_value = {
                    "ready": False,
                    "activation_events": 0,
                    "entity_edges": 0,
                    "frame_instances": 0,
                }
                patched["_get_graph_status_snapshot"].return_value = {
                    "ready": False,
                    "entities": 0,
                    "episode_mentions": 0,
                    "temporal_facts": 0,
                }
                patched["_get_llm_status_snapshot"].return_value = {}
                patched["_get_gmail_status_snapshot"].return_value = {}
                patched["_get_keepalive_snapshot"].return_value = {
                    "last_success_at": None,
                    "last_success_age_s": None,
                    "last_failure_at": None,
                    "last_failure_age_s": None,
                    "last_error": None,
                }
                patched["_keepalive_faiss_ping_sync"].side_effect = RuntimeError("faiss unavailable")

                payload = main._compute_health_payload(now=150.0)

            self.assertEqual(payload["status"], "degraded")
            self.assertEqual(payload["faiss_items"], 0)
            self.assertIn("faiss:", " ".join(payload["issues"]))
            self.assertFalse(payload["model_loaded"])
        finally:
            main._last_query_time = previous_query_time
            main._server_started_at = previous_started_at

    def test_health_payload_is_degraded_after_long_idle_without_keepalive(self) -> None:
        previous_query_time = main._last_query_time
        previous_started_at = main._server_started_at
        try:
            main._last_query_time = 0.0
            main._server_started_at = 0.0
            with self._patch_health_dependencies() as patched, mock.patch.object(
                main.embedder,
                "get_model_pool_status",
                return_value={
                    "initialized": True,
                    "pool_size": 2,
                    "requested_pool_size": 2,
                    "query_pool_initialized": True,
                    "query_pool_size": 1,
                    "query_pool_requested_size": 1,
                },
            ):
                patched["_get_count_summary"].return_value = {
                    "unique_memories": 8,
                    "files_watched": 2,
                    "duplicates_skipped": 0,
                }
                patched["_get_db_item_count"].return_value = 8
                patched["_get_pending_watched_file_count"].return_value = 0
                patched["_status_scan_snapshot"].return_value = ({}, {}, False)
                patched["_get_cognitive_status_snapshot"].return_value = {
                    "ready": True,
                    "activation_events": 0,
                    "entity_edges": 0,
                    "frame_instances": 0,
                }
                patched["_get_graph_status_snapshot"].return_value = {
                    "ready": True,
                    "entities": 0,
                    "episode_mentions": 0,
                    "temporal_facts": 0,
                }
                patched["_get_llm_status_snapshot"].return_value = {}
                patched["_get_gmail_status_snapshot"].return_value = {}
                patched["_get_keepalive_snapshot"].return_value = {
                    "last_success_at": None,
                    "last_success_age_s": None,
                    "last_failure_at": None,
                    "last_failure_age_s": None,
                    "last_error": None,
                }
                patched["_keepalive_faiss_ping_sync"].return_value = 8

                payload = main._compute_health_payload(
                    now=float(main.HEALTH_DEGRADED_IDLE_SECONDS + 10)
                )

            self.assertEqual(payload["status"], "degraded")
            self.assertGreater(payload["last_query_age_s"], main.HEALTH_DEGRADED_IDLE_SECONDS)
        finally:
            main._last_query_time = previous_query_time
            main._server_started_at = previous_started_at

    def test_retryable_db_startup_error_detects_locked_sqlite(self) -> None:
        self.assertTrue(
            main._is_retryable_db_startup_error(sqlite3.OperationalError("database is locked"))
        )
        self.assertTrue(
            main._is_retryable_db_startup_error(Exception("database schema is locked"))
        )
        self.assertFalse(main._is_retryable_db_startup_error(RuntimeError("disk image malformed")))

    def test_startup_health_does_not_reset_db_on_retryable_lock(self) -> None:
        fake_cognitive_store = mock.Mock()
        fake_cognitive_store.get_counts.return_value = {
            "activation_events": 0,
            "entity_edges": 0,
            "frame_instances": 0,
        }
        with (
            mock.patch.object(main.database, "init_db") as init_db,
            mock.patch.object(
                main.database,
                "get_connection",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            mock.patch.object(main, "_reset_database_files") as reset_db,
            mock.patch.object(main, "_get_graph_status_snapshot", return_value={"ready": True}),
            mock.patch.object(main.cognitive_store, "init_db"),
            mock.patch.object(main.cognitive_store, "get_store", return_value=fake_cognitive_store),
            mock.patch.object(main.config, "TEXT_FAISS_PATH", Path("/tmp/recurdo-test-missing-faiss.index")),
        ):
            result = asyncio.run(main.startup_health_check())

        init_db.assert_called_once()
        reset_db.assert_not_called()
        self.assertIn("DB error: database is locked", result["issues"])
        self.assertIn("deferred_db_repair", result["actions"])
        self.assertFalse(result["cognitive_counts"]["activation_events"])

    def test_startup_health_resets_db_on_non_retryable_db_error(self) -> None:
        fake_conn = mock.Mock()
        fake_conn.close = mock.Mock()
        fake_cognitive_store = mock.Mock()
        fake_cognitive_store.get_counts.return_value = {
            "activation_events": 0,
            "entity_edges": 0,
            "frame_instances": 0,
        }
        with (
            mock.patch.object(main.database, "init_db") as init_db,
            mock.patch.object(
                main.database,
                "get_connection",
                side_effect=[RuntimeError("disk image malformed"), fake_conn],
            ),
            mock.patch.object(main.database, "reconcile_pending_files", return_value={
                "total": 0,
                "still_pending": 0,
                "marked_missing": 0,
                "marked_failed": 0,
                "too_old": 0,
            }),
            mock.patch.object(main.database, "get_watched_folders", return_value=[]),
            mock.patch.object(main, "_reset_database_files") as reset_db,
            mock.patch.object(main, "_get_graph_status_snapshot", return_value={"ready": True}),
            mock.patch.object(main.cognitive_store, "init_db"),
            mock.patch.object(main.cognitive_store, "get_store", return_value=fake_cognitive_store),
            mock.patch.object(main.config, "TEXT_FAISS_PATH", Path("/tmp/recurdo-test-missing-faiss.index")),
        ):
            result = asyncio.run(main.startup_health_check())

        self.assertEqual(init_db.call_count, 2)
        reset_db.assert_called_once()
        self.assertIn("recreated_db", result["actions"])


if __name__ == "__main__":
    unittest.main()
