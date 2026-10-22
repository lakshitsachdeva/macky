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

from backend import auth, custody, database, forensic, main


class ForensicAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "memory.db"
        database.DB_PATH = self.db_path
        database.config.DB_PATH = self.db_path
        database.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_case(self, name: str) -> dict:
        return asyncio.run(
            main.api_create_case(
                main.CaseCreateRequest(name=name),
                user={"id": "tester"},
            )
        )

    def _add_evidence(
        self,
        case_id: str,
        *,
        item_id: str,
        filename: str,
        content: str,
        modified_at: str,
        first_seen_at: str,
    ) -> Path:
        source = self.root / filename
        source.write_text(content, encoding="utf-8")
        stat_result = source.stat()
        conn = database.get_connection()
        try:
            database.insert_item(
                conn,
                {
                    "id": item_id,
                    "content_hash": f"content-{item_id}",
                    "title": filename,
                    "filename": filename,
                    "file_type": "text",
                    "handler_type": "text",
                    "source_type": "local",
                    "original_path": str(source),
                    "created_at": first_seen_at,
                    "modified_at": modified_at,
                    "first_seen_at": first_seen_at,
                    "file_mtime": stat_result.st_mtime,
                    "file_size_bytes": stat_result.st_size,
                    "extracted_text": content,
                    "preview_text": content[:80],
                    "summary": f"Summary for {filename}",
                },
            )
            database.upsert_case_evidence(
                conn,
                {
                    "case_id": case_id,
                    "item_id": item_id,
                    "source_path": str(source),
                    "sha256": custody.hash_file_streaming(source),
                    "size_bytes": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "imported_at": first_seen_at,
                    "acquisition_status": "acquired",
                },
            )
        finally:
            conn.close()
        return source

    def _case_rows(self, case_id: str) -> tuple[list[dict], list[dict]]:
        conn = database.get_connection()
        try:
            return (
                database.list_case_evidence(conn, case_id),
                database.list_custody_events(conn, case_id),
            )
        finally:
            conn.close()

    def test_forensic_timeline_isolated_to_case_evidence(self) -> None:
        case_a = self._create_case("Case A")
        case_b = self._create_case("Case B")
        source_a = self._add_evidence(
            case_a["id"],
            item_id="item-a",
            filename="alpha.txt",
            content="alpha evidence",
            modified_at="2026-01-02T00:00:00+00:00",
            first_seen_at="2026-01-03T00:00:00+00:00",
        )
        self._add_evidence(
            case_b["id"],
            item_id="item-b",
            filename="beta.txt",
            content="beta evidence",
            modified_at="2026-01-04T00:00:00+00:00",
            first_seen_at="2026-01-05T00:00:00+00:00",
        )

        conn = database.get_connection()
        try:
            database.append_custody_event(
                conn,
                case_id=case_a["id"],
                item_id=None,
                event_type="case_note",
                actor="tester",
                occurred_at="2026-01-01T00:00:00+00:00",
                details={"note": "case level"},
                event_id="case-a-note",
            )
            # This malformed cross-case association must not leak item-b into
            # case A's forensic view.
            database.append_custody_event(
                conn,
                case_id=case_a["id"],
                item_id="item-b",
                event_type="unexpected_item",
                actor="tester",
                occurred_at="2026-01-06T00:00:00+00:00",
                details={},
                event_id="case-a-out-of-scope",
            )
        finally:
            conn.close()

        timeline = asyncio.run(
            main.api_case_forensic_timeline(
                case_a["id"],
                start_at=None,
                end_at=None,
                limit=1000,
                user={"id": "tester"},
            )
        )
        serialized = json.dumps(timeline, sort_keys=True)
        self.assertIn("item-a", serialized)
        self.assertNotIn("item-b", serialized)
        self.assertIn(str(source_a), serialized)
        self.assertEqual(timeline["evidence_count"], 1)
        self.assertEqual(timeline["custody_event_count"], 1)
        self.assertEqual(
            [entry["timeline_type"] for entry in timeline["entries"]],
            ["custody", "evidence"],
        )
        evidence_entry = next(entry for entry in timeline["entries"] if entry["timeline_type"] == "evidence")
        self.assertEqual(evidence_entry["evidence"]["acquisition_status"], "acquired")
        self.assertTrue(evidence_entry["evidence"]["sha256"])
        self.assertEqual(
            evidence_entry["item"]["timestamps"]["modified_at"],
            "2026-01-02T00:00:00+00:00",
        )

        report = asyncio.run(
            main.api_case_report(
                case_a["id"],
                main.ForensicReportRequest(question="Review case A", allow_model=False, limit=10),
                user={"id": "tester"},
            )
        )
        report_serialized = json.dumps(report, sort_keys=True)
        self.assertIn("item-a", report_serialized)
        self.assertNotIn("item-b", report_serialized)

    def test_forensic_timeline_supports_exact_bounds(self) -> None:
        case = self._create_case("Bounded case")
        self._add_evidence(
            case["id"],
            item_id="bounded-item",
            filename="bounded.txt",
            content="bounded evidence",
            modified_at="2026-02-02T00:00:00+00:00",
            first_seen_at="2026-02-03T00:00:00+00:00",
        )
        timeline = asyncio.run(
            main.api_case_forensic_timeline(
                case["id"],
                start_at="2026-02-03T00:00:00Z",
                end_at="2026-02-03T00:00:00Z",
                limit=1000,
                user={"id": "tester"},
            )
        )
        self.assertEqual(len(timeline["entries"]), 1)
        self.assertEqual(timeline["bounds"]["start_at"], "2026-02-03T00:00:00+00:00")

    def test_registered_forensic_routes_work_through_local_http_access(self) -> None:
        case = self._create_case("HTTP forensic case")
        self._add_evidence(
            case["id"],
            item_id="http-item",
            filename="http.txt",
            content="HTTP route evidence",
            modified_at="2026-02-04T00:00:00+00:00",
            first_seen_at="2026-02-04T00:00:00+00:00",
        )

        async def run() -> tuple[int, int, dict, dict]:
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                timeline = await client.get(f"/cases/{case['id']}/forensic-timeline")
                report = await client.post(
                    f"/cases/{case['id']}/report",
                    json={"question": "What is recorded?", "allow_model": False, "limit": 1},
                )
                return timeline.status_code, report.status_code, timeline.json(), report.json()

        with mock.patch.object(auth, "LOCAL_MODE", True), mock.patch.object(
            auth, "ALLOW_LOCAL_DEV_AUTH", True
        ):
            timeline_status, report_status, timeline, report = asyncio.run(run())
        self.assertEqual(timeline_status, 200)
        self.assertEqual(report_status, 200)
        self.assertEqual(timeline["case_id"], case["id"])
        self.assertEqual(report["case_id"], case["id"])

    def test_report_contains_provenance_citations_and_custody_context(self) -> None:
        case = self._create_case("Report case")
        source = self._add_evidence(
            case["id"],
            item_id="report-item",
            filename="report.txt",
            content="The indexed report says the handoff occurred at 09:00.",
            modified_at="2026-03-02T09:00:00+00:00",
            first_seen_at="2026-03-02T10:00:00+00:00",
        )
        conn = database.get_connection()
        try:
            database.append_custody_event(
                conn,
                case_id=case["id"],
                item_id="report-item",
                event_type="evidence_verified",
                actor="tester",
                occurred_at="2026-03-02T11:00:00+00:00",
                details={"status": "verified"},
                event_id="report-verify",
            )
        finally:
            conn.close()

        response = asyncio.run(
            main.api_case_report(
                case["id"],
                main.ForensicReportRequest(
                    question="What is recorded about the handoff?",
                    allow_model=False,
                    limit=5,
                ),
                user={"id": "tester"},
            )
        )
        source_payload = response["sources"][0]
        self.assertEqual(source_payload["item_id"], "report-item")
        self.assertEqual(source_payload["source_path"], str(source))
        self.assertEqual(source_payload["source_name"], "report.txt")
        self.assertEqual(source_payload["sha256"], custody.hash_file_streaming(source))
        self.assertEqual(source_payload["acquisition_status"], "acquired")
        self.assertEqual(source_payload["modified_at"], "2026-03-02T09:00:00+00:00")
        self.assertEqual(source_payload["verification"]["status"], "verified")
        self.assertEqual(source_payload["custody_context"][0]["event_type"], "evidence_verified")
        self.assertEqual(response["report"]["findings"][0]["source_indices"], [1])
        self.assertEqual(response["generation"]["provider"], "heuristic")
        self.assertTrue(response["generation"]["fallback_used"])
        self.assertNotIn("key", json.dumps(response).lower())

    def test_allow_model_false_never_constructs_or_calls_llm(self) -> None:
        case = self._create_case("Fallback case")
        self._add_evidence(
            case["id"],
            item_id="fallback-item",
            filename="fallback.txt",
            content="fallback evidence",
            modified_at="2026-04-01T00:00:00+00:00",
            first_seen_at="2026-04-01T00:00:00+00:00",
        )
        with mock.patch.object(
            forensic,
            "LocalLLM",
            side_effect=AssertionError("LLM must not be constructed when allow_model is false"),
        ):
            response = asyncio.run(
                main.api_case_report(
                    case["id"],
                    main.ForensicReportRequest(
                        prompt="Summarize the recorded evidence.",
                        allow_model=False,
                        limit=1,
                    ),
                    user={"id": "tester"},
                )
            )
        self.assertEqual(response["generation"]["fallback_reason"], "model_not_requested")
        self.assertEqual(response["source_count"], 1)

    def test_empty_case_uses_deterministic_empty_report(self) -> None:
        case = self._create_case("Empty case")
        with mock.patch.object(
            forensic,
            "LocalLLM",
            side_effect=AssertionError("empty case should not call an LLM"),
        ):
            response = asyncio.run(
                main.api_case_report(
                    case["id"],
                    main.ForensicReportRequest(
                        question="Is there any evidence?",
                        allow_model=True,
                        limit=5,
                    ),
                    user={"id": "tester"},
                )
            )
        self.assertEqual(response["source_count"], 0)
        self.assertEqual(response["sources"], [])
        self.assertEqual(response["report"]["findings"], [])
        self.assertEqual(response["generation"]["fallback_reason"], "empty_case")
        self.assertEqual(response["warnings"], [])

    def test_report_hash_is_deterministic_for_same_fallback_inputs(self) -> None:
        case = self._create_case("Hash case")
        self._add_evidence(
            case["id"],
            item_id="hash-item",
            filename="hash.txt",
            content="deterministic evidence",
            modified_at="2026-05-01T00:00:00+00:00",
            first_seen_at="2026-05-01T00:00:00+00:00",
        )
        request = main.ForensicReportRequest(
            question="What is recorded?",
            allow_model=False,
            limit=5,
        )
        first = asyncio.run(main.api_case_report(case["id"], request, user={"id": "tester"}))
        second = asyncio.run(main.api_case_report(case["id"], request, user={"id": "tester"}))
        self.assertEqual(first["report_sha256"], second["report_sha256"])
        self.assertEqual(
            first["report_sha256"],
            hashlib.sha256(
                database.canonical_json(
                    {
                        "case": first["case"],
                        "question": first["question"],
                        "report": first["report"],
                        "sources": first["sources"],
                        "custody_events": first["custody_events"],
                        "warnings": first["warnings"],
                    }
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_changed_missing_and_unreadable_evidence_are_warned(self) -> None:
        case = self._create_case("Warning case")
        changed = self._add_evidence(
            case["id"],
            item_id="changed-item",
            filename="changed.txt",
            content="original",
            modified_at="2026-06-01T00:00:00+00:00",
            first_seen_at="2026-06-01T00:00:00+00:00",
        )
        missing = self._add_evidence(
            case["id"],
            item_id="missing-item",
            filename="missing.txt",
            content="will disappear",
            modified_at="2026-06-02T00:00:00+00:00",
            first_seen_at="2026-06-02T00:00:00+00:00",
        )
        unreadable = self.root / "unreadable-target"
        unreadable.mkdir()
        conn = database.get_connection()
        try:
            stat_result = unreadable.stat()
            database.insert_item(
                conn,
                {
                    "id": "unreadable-item",
                    "content_hash": "content-unreadable-item",
                    "filename": unreadable.name,
                    "original_path": str(unreadable),
                    "first_seen_at": "2026-06-03T00:00:00+00:00",
                    "modified_at": "2026-06-03T00:00:00+00:00",
                },
            )
            database.upsert_case_evidence(
                conn,
                {
                    "case_id": case["id"],
                    "item_id": "unreadable-item",
                    "source_path": str(unreadable),
                    "sha256": "not-a-directory-hash",
                    "size_bytes": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "imported_at": "2026-06-03T00:00:00+00:00",
                    "acquisition_status": "acquired",
                },
            )
        finally:
            conn.close()
        changed.write_text("changed", encoding="utf-8")
        missing.unlink()

        response = asyncio.run(
            main.api_case_report(
                case["id"],
                main.ForensicReportRequest(question="Review integrity", allow_model=False, limit=10),
                user={"id": "tester"},
            )
        )
        warning_text = " ".join(response["warnings"])
        self.assertIn("changed", warning_text)
        self.assertIn("missing", warning_text)
        self.assertIn("unreadable", warning_text)

    def test_invalid_model_citations_force_fallback(self) -> None:
        case = self._create_case("Model validation case")
        self._add_evidence(
            case["id"],
            item_id="model-item",
            filename="model.txt",
            content="model validation evidence",
            modified_at="2026-07-01T00:00:00+00:00",
            first_seen_at="2026-07-01T00:00:00+00:00",
        )

        class FakeLLM:
            def status(self) -> dict:
                return {"available": True, "provider": "fake-local"}

            async def generate(self, *_args, **_kwargs) -> str:
                return json.dumps(
                    {
                        "summary": "Unsupported claim.",
                        "summary_source_indices": [99],
                        "findings": [],
                        "limitations": [],
                    }
                )

        evidence_rows, custody_rows = self._case_rows(case["id"])
        response = asyncio.run(
            forensic.build_forensic_report(
                case=case,
                question="Validate citations",
                evidence_rows=evidence_rows,
                custody_rows=custody_rows,
                allow_model=True,
                limit=5,
                llm=FakeLLM(),
            )
        )
        self.assertFalse(response["generation"]["model_used"])
        self.assertEqual(
            response["generation"]["fallback_reason"],
            "invalid_model_output_or_citations",
        )
        self.assertEqual(response["report"]["findings"][0]["source_indices"], [1])

    def test_allowed_local_model_output_is_structured_and_cited(self) -> None:
        case = self._create_case("Model success case")
        self._add_evidence(
            case["id"],
            item_id="model-success-item",
            filename="model-success.txt",
            content="model success evidence",
            modified_at="2026-08-01T00:00:00+00:00",
            first_seen_at="2026-08-01T00:00:00+00:00",
        )

        class FakeLLM:
            def status(self) -> dict:
                return {"available": True, "provider": "fake-local", "api_key": "should-not-leak"}

            async def generate(self, *_args, **_kwargs) -> str:
                return json.dumps(
                    {
                        "summary": "The record contains one cited source.",
                        "summary_source_indices": [1],
                        "findings": [
                            {
                                "statement": "The source is present in the case manifest.",
                                "source_indices": [1],
                            }
                        ],
                        "limitations": [],
                    }
                )

        evidence_rows, custody_rows = self._case_rows(case["id"])
        response = asyncio.run(
            forensic.build_forensic_report(
                case=case,
                question="Use the local model",
                evidence_rows=evidence_rows,
                custody_rows=custody_rows,
                allow_model=True,
                limit=5,
                llm=FakeLLM(),
            )
        )
        self.assertTrue(response["generation"]["model_used"])
        self.assertFalse(response["generation"]["fallback_used"])
        self.assertEqual(response["provider"], "fake-local")
        self.assertEqual(response["report"]["summary_source_indices"], [1])
        self.assertNotIn("should-not-leak", json.dumps(response))

    def test_model_prompt_withholds_absolute_source_paths(self) -> None:
        case = self._create_case("Prompt privacy case")
        source = self._add_evidence(
            case["id"],
            item_id="prompt-privacy-item",
            filename="prompt-privacy.txt",
            content="prompt privacy evidence",
            modified_at="2026-09-01T00:00:00+00:00",
            first_seen_at="2026-09-01T00:00:00+00:00",
        )

        class CapturingLLM:
            def __init__(self) -> None:
                self.prompt = ""

            def status(self) -> dict:
                return {"available": True, "provider": "fake-local"}

            async def generate(self, prompt: str, *_args, **_kwargs) -> str:
                self.prompt = prompt
                return json.dumps(
                    {
                        "summary": "The source is present.",
                        "summary_source_indices": [1],
                        "findings": [{"statement": "The source is recorded.", "source_indices": [1]}],
                        "limitations": [],
                    }
                )

        llm = CapturingLLM()
        evidence_rows, custody_rows = self._case_rows(case["id"])
        asyncio.run(
            forensic.build_forensic_report(
                case=case,
                question="Review source privacy",
                evidence_rows=evidence_rows,
                custody_rows=custody_rows,
                allow_model=True,
                limit=5,
                llm=llm,
            )
        )
        self.assertIn("source_name=prompt-privacy.txt", llm.prompt)
        self.assertNotIn(str(source), llm.prompt)


if __name__ == "__main__":
    unittest.main()
