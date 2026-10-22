from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

os.environ["RECURDO_LOCAL_MODE"] = "1"

from backend import auth, custody, database, main
from backend.retrieval import query_parser, search


class CustodyWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "memory.db"
        database.DB_PATH = self.db_path
        database.config.DB_PATH = self.db_path
        database.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_case(self, name: str = "Evidence review") -> dict:
        return asyncio.run(
            main.api_create_case(
                main.CaseCreateRequest(name=name, description="Description"),
                user={"id": "tester"},
            )
        )

    def test_init_db_creates_case_provenance_tables(self) -> None:
        conn = database.get_connection()
        try:
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertTrue({"cases", "case_evidence", "custody_events"} <= tables)
        expected_columns = {
            "cases": {"id", "name", "description", "status", "created_at", "updated_at", "notes"},
            "case_evidence": {
                "case_id",
                "item_id",
                "source_path",
                "sha256",
                "size_bytes",
                "mtime_ns",
                "imported_at",
                "acquisition_status",
            },
            "custody_events": {
                "id",
                "case_id",
                "item_id",
                "event_type",
                "actor",
                "occurred_at",
                "details_json",
                "previous_event_hash",
                "event_hash",
            },
        }
        conn = database.get_connection()
        try:
            for table, columns in expected_columns.items():
                actual = {
                    str(row["name"])
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                self.assertEqual(actual, columns)
        finally:
            conn.close()

    def test_custody_event_hash_is_deterministic_and_chained(self) -> None:
        first_details = database.canonical_json({"b": 2, "a": 1})
        equivalent_details = database.canonical_json({"a": 1, "b": 2})
        first_payload = {
            "id": "event-1",
            "case_id": "case-1",
            "item_id": "item-1",
            "event_type": "evidence_acquired",
            "actor": "tester",
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "details_json": first_details,
            "previous_event_hash": None,
        }
        equivalent_payload = {**first_payload, "details_json": equivalent_details}
        self.assertEqual(
            database.compute_custody_event_hash(first_payload),
            database.compute_custody_event_hash(equivalent_payload),
        )

        conn = database.get_connection()
        try:
            first = database.append_custody_event(
                conn,
                case_id="case-1",
                item_id="item-1",
                event_type="evidence_acquired",
                actor="tester",
                occurred_at="2026-01-01T00:00:00+00:00",
                details={"a": 1, "b": 2},
                event_id="event-1",
            )
            second = database.append_custody_event(
                conn,
                case_id="case-1",
                item_id="item-1",
                event_type="evidence_verified",
                actor="tester",
                occurred_at="2026-01-01T00:00:01+00:00",
                details={"status": "verified"},
                event_id="event-2",
            )
        finally:
            conn.close()

        self.assertIsNone(first["previous_event_hash"])
        self.assertEqual(second["previous_event_hash"], first["event_hash"])
        self.assertEqual(database.compute_custody_event_hash(second), second["event_hash"])

    def test_streaming_hash_reads_fixed_size_chunks(self) -> None:
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.__exit__.return_value = False
        handle.read.side_effect = [b"ab", b"cd", b""]

        with mock.patch.object(Path, "open", return_value=handle) as open_mock:
            digest = custody.hash_file_streaming(self.root / "large.bin", chunk_size=2)

        self.assertEqual(digest, hashlib.sha256(b"abcd").hexdigest())
        open_mock.assert_called_once_with("rb")
        self.assertEqual(handle.read.call_args_list, [mock.call(2), mock.call(2), mock.call(2)])

    def test_streaming_hash_matches_a_real_temp_file(self) -> None:
        source = self.root / "streamed.bin"
        payload = (b"custody-evidence\n" * 4096) + b"final-byte"
        source.write_bytes(payload)

        self.assertEqual(
            custody.hash_file_streaming(source, chunk_size=17),
            hashlib.sha256(payload).hexdigest(),
        )

    def test_case_crud_round_trip(self) -> None:
        created = self._create_case()
        self.assertEqual(created["name"], "Evidence review")

        listed = asyncio.run(main.api_list_cases(user={"id": "tester"}))
        self.assertEqual([case["id"] for case in listed], [created["id"]])

        patched = asyncio.run(
            main.api_patch_case(
                created["id"],
                main.CasePatchRequest(status="closed", notes="Reviewed"),
                user={"id": "tester"},
            )
        )
        self.assertEqual(patched["status"], "closed")
        self.assertEqual(patched["notes"], "Reviewed")

        fetched = asyncio.run(main.api_get_case(created["id"], user={"id": "tester"}))
        self.assertEqual(fetched["id"], created["id"])
        asyncio.run(main.api_delete_case(created["id"], user={"id": "tester"}))
        with self.assertRaises(main.HTTPException) as error:
            asyncio.run(main.api_get_case(created["id"], user={"id": "tester"}))
        self.assertEqual(error.exception.status_code, 404)

    def test_case_routes_work_through_local_access_dependency(self) -> None:
        async def run() -> tuple[int, int, dict]:
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                created = await client.post(
                    "/cases",
                    json={"name": "HTTP case", "notes": "local"},
                )
                listed = await client.get("/cases")
                return created.status_code, listed.status_code, created.json()

        with mock.patch.object(auth, "LOCAL_MODE", True), mock.patch.object(
            auth, "ALLOW_LOCAL_DEV_AUTH", True
        ):
            created_status, listed_status, created = asyncio.run(run())
        self.assertEqual(created_status, 200)
        self.assertEqual(listed_status, 200)
        self.assertEqual(created["name"], "HTTP case")

    def test_import_and_verify_detects_source_modification(self) -> None:
        case = self._create_case()
        source = self.root / "evidence.txt"
        source.write_bytes(b"original evidence")

        with mock.patch.object(
            main.pipeline,
            "index_file_safe",
            new=mock.AsyncMock(return_value={"status": "indexed", "item_id": "item-1"}),
        ):
            imported = asyncio.run(
                main._import_case_evidence(
                    case_id=case["id"],
                    paths=[str(source)],
                    actor="tester",
                    hash_algorithm="sha256",
                )
            )

        self.assertEqual(imported["files"][0]["status"], "indexed")
        self.assertEqual(source.read_bytes(), b"original evidence")

        source.write_bytes(b"modified evidence")
        verification = asyncio.run(
            main.api_verify_case(case["id"], user={"id": "tester"})
        )
        self.assertEqual(verification["verified"], 0)
        self.assertEqual(verification["changed"], 1)
        self.assertEqual(verification["files"][0]["status"], "changed")

        conn = database.get_connection()
        try:
            events = database.list_custody_events(conn, case["id"])
        finally:
            conn.close()
        self.assertEqual(
            [event["event_type"] for event in events],
            ["evidence_acquired", "evidence_verified"],
        )

    def test_import_skips_missing_source_without_creating_evidence(self) -> None:
        case = self._create_case("Missing source")
        missing = self.root / "does-not-exist.txt"
        result = asyncio.run(
            main._import_case_evidence(
                case_id=case["id"],
                paths=[str(missing)],
                actor="tester",
                hash_algorithm="sha256",
            )
        )

        self.assertEqual(
            result["files"],
            [{"path": str(missing.resolve()), "status": "missing"}],
        )
        conn = database.get_connection()
        try:
            self.assertEqual(database.list_case_evidence(conn, case["id"]), [])
            self.assertEqual(database.list_custody_events(conn, case["id"]), [])
        finally:
            conn.close()

    def test_export_manifest_hash_is_canonical_and_json_safe(self) -> None:
        case = self._create_case("Export case")
        source = self.root / "report.txt"
        source.write_text("report", encoding="utf-8")
        with mock.patch.object(
            main.pipeline,
            "index_file_safe",
            new=mock.AsyncMock(return_value={"status": "indexed", "item_id": "item-export"}),
        ):
            asyncio.run(
                main._import_case_evidence(
                    case_id=case["id"],
                    paths=[str(source)],
                    actor="tester",
                    hash_algorithm="sha256",
                )
            )

        exported = asyncio.run(main.api_export_case(case["id"], user={"id": "tester"}))
        manifest = {
            "case": exported["case"],
            "evidence": exported["evidence"],
            "events": exported["events"],
        }
        expected_hash = hashlib.sha256(
            database.canonical_json(manifest).encode("utf-8")
        ).hexdigest()
        self.assertEqual(exported["manifest_sha256"], expected_hash)
        json.dumps(exported)

    def test_search_serializer_and_case_scope_include_provenance(self) -> None:
        case_a = self._create_case("Case A")
        case_b = self._create_case("Case B")
        first_path = self.root / "alpha.txt"
        second_path = self.root / "beta.txt"
        first_path.write_text("alpha evidence", encoding="utf-8")
        second_path.write_text("alpha unrelated", encoding="utf-8")

        conn = database.get_connection()
        try:
            for item_id, path, content_hash in (
                ("item-a", first_path, "hash-a"),
                ("item-b", second_path, "hash-b"),
            ):
                database.insert_item(
                    conn,
                    {
                        "id": item_id,
                        "content_hash": content_hash,
                        "title": path.stem,
                        "filename": path.name,
                        "file_type": "text",
                        "handler_type": "text",
                        "source_type": "local",
                        "original_path": str(path),
                        "first_seen_at": "2026-01-01T00:00:00+00:00",
                        "extracted_text": "alpha evidence",
                        "summary": "alpha summary",
                    },
                )
            database.upsert_case_evidence(
                conn,
                {
                    "case_id": case_a["id"],
                    "item_id": "item-a",
                    "source_path": str(first_path),
                    "sha256": "evidence-sha-a",
                    "size_bytes": 14,
                    "mtime_ns": 11,
                    "imported_at": "2026-01-01T00:00:00+00:00",
                    "acquisition_status": "acquired",
                },
            )
            database.upsert_case_evidence(
                conn,
                {
                    "case_id": case_b["id"],
                    "item_id": "item-b",
                    "source_path": str(second_path),
                    "sha256": "evidence-sha-b",
                    "size_bytes": 15,
                    "mtime_ns": 12,
                    "imported_at": "2026-01-01T00:00:00+00:00",
                    "acquisition_status": "acquired",
                },
            )
            scoped_fts = database.search_fts(conn, "alpha", case_id=case_a["id"])
            provenance = database.get_item_provenance(conn, ["item-a"])["item-a"]
        finally:
            conn.close()

        self.assertEqual([row["id"] for row in scoped_fts], ["item-a"])
        result = search.SearchResult(
            item_id="item-a",
            item={
                "id": "item-a",
                "filename": first_path.name,
                "original_path": str(first_path),
            },
            final_score=0.8,
        )
        serialized = main._serialize_search_results(
            [result],
            parsed=query_parser.parse_query("alpha"),
            limit=1,
        )
        self.assertEqual(serialized[0]["case_ids"], [case_a["id"]])
        self.assertEqual(serialized[0]["evidence_sha256"], "evidence-sha-a")
        self.assertEqual(serialized[0]["acquisition_status"], "acquired")

        self.assertEqual(provenance["case_ids"], [case_a["id"]])

        with mock.patch.object(
            search.embedder,
            "get_model_pool_status",
            return_value={"query_pool_initialized": False},
        ), mock.patch.object(search.embedder, "ensure_model_pool_warmup_started"):
            scoped_results = asyncio.run(
                search.search("alpha", limit=10, case_id=case_a["id"])
            )
        self.assertEqual([result.item_id for result in scoped_results], ["item-a"])

    def test_ask_preserves_case_scope_for_semantic_and_temporal_paths(self) -> None:
        case = self._create_case("Scoped Ask")
        source = self.root / "ask.txt"
        source.write_text("scoped ask evidence", encoding="utf-8")
        conn = database.get_connection()
        try:
            database.insert_item(
                conn,
                {
                    "id": "ask-item",
                    "content_hash": "ask-hash",
                    "title": "Ask evidence",
                    "filename": source.name,
                    "file_type": "text",
                    "handler_type": "text",
                    "source_type": "local",
                    "original_path": str(source),
                    "first_seen_at": "2026-01-01T00:00:00+00:00",
                    "extracted_text": "scoped ask evidence",
                    "summary": "scoped ask summary",
                },
            )
            database.upsert_case_evidence(
                conn,
                {
                    "case_id": case["id"],
                    "item_id": "ask-item",
                    "source_path": str(source),
                    "sha256": "ask-evidence-sha",
                    "size_bytes": source.stat().st_size,
                    "mtime_ns": source.stat().st_mtime_ns,
                    "imported_at": "2026-01-01T00:00:00+00:00",
                    "acquisition_status": "acquired",
                },
            )
        finally:
            conn.close()

        result = search.SearchResult(
            item_id="ask-item",
            item={
                "id": "ask-item",
                "filename": source.name,
                "original_path": str(source),
                "summary": "scoped ask summary",
            },
            final_score=0.9,
        )

        async def run() -> tuple[dict, mock.AsyncMock]:
            search_mock = mock.AsyncMock(return_value=[result])
            synthesis_mock = mock.AsyncMock(
                return_value={
                    "text": "Scoped answer",
                    "provider": "heuristic",
                    "confidence": "high",
                }
            )
            with mock.patch.object(main.search, "search", search_mock), mock.patch.object(
                main, "synthesize_ask_answer", synthesis_mock
            ):
                response = await main.ask_question(
                    main.AskRequest(
                        question="what is scoped?",
                        case_id=case["id"],
                        allow_model=False,
                    ),
                    user={"id": "tester", "email": "tester@example.com"},
                )
            return response, search_mock

        response, search_mock = asyncio.run(run())
        self.assertEqual(search_mock.await_args.kwargs["case_id"], case["id"])
        self.assertEqual(response["sources"][0]["item_id"], "ask-item")


if __name__ == "__main__":
    unittest.main()
