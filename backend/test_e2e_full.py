#!/usr/bin/env python3
"""
Full E2E test: index -> search -> retrieve -> open.

Usage:
    python test_e2e_full.py --token YOUR_TOKEN
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx


BASE = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_INDEX_WAIT_SECONDS = 45.0


def format_detail(detail: Any) -> str:
    if isinstance(detail, (dict, list)):
        return json.dumps(detail, ensure_ascii=True, sort_keys=True)
    return str(detail)


def response_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        body = response.text.strip()
    return f"status={response.status_code} body={format_detail(body)}"


async def wait_for_search_hit(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    base_url: str,
    query: str,
    expected_filename: str,
    timeout_seconds: float,
) -> tuple[bool, list[dict[str, Any]], str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_detail = "search did not run"

    while asyncio.get_running_loop().time() < deadline:
        response = await client.post(
            f"{base_url}/search",
            headers=headers,
            json={"query": query, "limit": 10},
        )

        if response.status_code == 200:
            try:
                hits = response.json()
            except Exception as exc:
                last_detail = repr(exc)
                await asyncio.sleep(1)
                continue

            filenames = [str(hit.get("filename") or "") for hit in hits]
            if any(expected_filename == name for name in filenames):
                return True, hits, f"found in {len(hits)} hits"
            last_detail = f"latest hits={filenames[:5]}"
        else:
            last_detail = response_detail(response)

        await asyncio.sleep(1)

    return False, [], last_detail


def print_results(results: list[tuple[str, bool, Any]]) -> None:
    print()
    print("=" * 72)
    print("RECURDO FULL E2E TEST RESULTS")
    print("=" * 72)

    passed = 0
    failed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        if not ok:
            print(f"        got: {format_detail(detail)}")
        passed += int(ok)
        failed += int(not ok)

    print("=" * 72)
    print(f"{passed} passed, {failed} failed")
    print("=" * 72)
    print()


async def run(args: argparse.Namespace) -> bool:
    base_url = args.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    results: list[tuple[str, bool, Any]] = []
    temp_dir_path: Path | None = None
    test_file_path: Path | None = None
    indexed_item: dict[str, Any] | None = None

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        try:
            health = await client.get(f"{base_url}/health")
            results.append(("E1 backend healthy", health.status_code == 200, response_detail(health)))

            auth = await client.get(f"{base_url}/auth/status", headers=headers)
            results.append(("E2 auth/status -> 200", auth.status_code == 200, response_detail(auth)))
            if auth.status_code != 200:
                print_results(results)
                return False

            auth_data = auth.json()
            user = auth_data.get("user") or {}
            access = auth_data.get("access") or {}
            results.append(("E2a user.email present", bool(user.get("email")), format_detail(user)))
            results.append(("E2b has_access is true", access.get("has_access") is True, format_detail(access)))

            temp_dir_path = Path(
                tempfile.mkdtemp(prefix="recurdo_e2e_", dir=str(Path.home()))
            ).resolve()
            unique_id = uuid.uuid4().hex[:10]
            filename = f"recurdo_e2e_{unique_id}_quantum.txt"
            test_file_path = temp_dir_path / filename
            test_file_path.write_text(
                (
                    "Recurdo E2E test document.\n"
                    "Topics: quantum mechanics, astrophysics, event horizons, and black holes.\n"
                    f"Marker: {unique_id}\n"
                ),
                encoding="utf-8",
            )
            results.append(("E3 test file created", True, str(test_file_path)))

            add_folder = await client.post(
                f"{base_url}/folders/add",
                headers=headers,
                json={"path": str(temp_dir_path), "label": f"Recurdo E2E {unique_id}"},
            )
            add_ok = add_folder.status_code == 200
            results.append(("E3a watched folder added", add_ok, response_detail(add_folder)))

            index_trigger = await client.post(f"{base_url}/index/trigger", headers=headers)
            results.append(
                (
                    "E3b index trigger accepted",
                    index_trigger.status_code == 200,
                    response_detail(index_trigger),
                )
            )

            found, initial_hits, wait_detail = await wait_for_search_hit(
                client=client,
                headers=headers,
                base_url=base_url,
                query=unique_id,
                expected_filename=filename,
                timeout_seconds=args.index_wait,
            )
            results.append(("E3c temp file indexed and searchable", found, wait_detail))

            search_exact = await client.post(
                f"{base_url}/search",
                headers=headers,
                json={"query": f"quantum mechanics {unique_id}", "limit": 5},
            )
            results.append(("E4 search returns 200", search_exact.status_code == 200, response_detail(search_exact)))

            exact_hits: list[dict[str, Any]] = []
            if search_exact.status_code == 200:
                exact_hits = search_exact.json()
                results.append(("E4a results non-empty", len(exact_hits) > 0, f"{len(exact_hits)} hits"))
                indexed_item = next(
                    (hit for hit in exact_hits if hit.get("filename") == filename),
                    next((hit for hit in initial_hits if hit.get("filename") == filename), None),
                )
                results.append(
                    (
                        "E4b temp file present in results",
                        indexed_item is not None,
                        [hit.get("filename") for hit in exact_hits[:5]],
                    )
                )
            else:
                results.append(("E4a results non-empty", False, "Skipped because E4 failed"))
                results.append(("E4b temp file present in results", False, "Skipped because E4 failed"))

            search_semantic = await client.post(
                f"{base_url}/search",
                headers=headers,
                json={"query": "astrophysics event horizons", "limit": 5},
            )
            results.append(
                ("E5 semantic-style search returns 200", search_semantic.status_code == 200, response_detail(search_semantic))
            )
            if search_semantic.status_code == 200:
                semantic_hits = search_semantic.json()
                results.append(("E5a semantic-style search gets results", len(semantic_hits) > 0, f"{len(semantic_hits)} hits"))
            else:
                results.append(("E5a semantic-style search gets results", False, "Skipped because E5 failed"))

            search_time = await client.post(
                f"{base_url}/search",
                headers=headers,
                json={"query": "file from today", "limit": 5},
            )
            results.append(
                ("E6 time-aware query handled", search_time.status_code == 200, response_detail(search_time))
            )

            items = await client.get(f"{base_url}/items", headers=headers, params={"limit": 50})
            results.append(("E7 /items -> 200", items.status_code == 200, response_detail(items)))
            if items.status_code == 200:
                items_data = items.json()
                item_rows = items_data.get("items") or []
                results.append(("E7a items count > 0", len(item_rows) > 0, f"{len(item_rows)} rows"))
                if indexed_item is None:
                    indexed_item = next(
                        (row for row in item_rows if row.get("filename") == filename),
                        None,
                    )
                results.append(
                    (
                        "E7b temp file present in /items",
                        indexed_item is not None,
                        [row.get("filename") for row in item_rows[:10]],
                    )
                )
            else:
                results.append(("E7a items count > 0", False, "Skipped because E7 failed"))
                results.append(("E7b temp file present in /items", False, "Skipped because E7 failed"))

            index_status = await client.get(f"{base_url}/index/status", headers=headers)
            results.append(("E8 /index/status -> 200", index_status.status_code == 200, response_detail(index_status)))

            if indexed_item and indexed_item.get("id"):
                item_id = str(indexed_item["id"])

                get_item = await client.get(f"{base_url}/items/{item_id}", headers=headers)
                results.append(("E9 retrieve indexed item -> 200", get_item.status_code == 200, response_detail(get_item)))

                open_item = await client.post(f"{base_url}/items/{item_id}/open", headers=headers)
                results.append(("E10 open indexed file -> 200", open_item.status_code == 200, response_detail(open_item)))
            else:
                results.append(("E9 retrieve indexed item -> 200", False, "No indexed item id available"))
                results.append(("E10 open indexed file -> 200", False, "No indexed item id available"))

            empty_query = await client.post(
                f"{base_url}/search",
                headers=headers,
                json={"query": "", "limit": 5},
            )
            results.append(
                (
                    "E11 empty query handled gracefully",
                    empty_query.status_code in (200, 400, 422),
                    response_detail(empty_query),
                )
            )

            long_query = await client.post(
                f"{base_url}/search",
                headers=headers,
                json={"query": "the " * 100, "limit": 5},
            )
            results.append(
                (
                    "E12 long query does not crash",
                    long_query.status_code in (200, 400, 422),
                    response_detail(long_query),
                )
            )
        finally:
            if temp_dir_path is not None:
                async with httpx.AsyncClient(timeout=args.timeout) as cleanup_client:
                    try:
                        remove_folder = await cleanup_client.request(
                            "DELETE",
                            f"{base_url}/folders/remove",
                            headers=headers,
                            json={"path": str(temp_dir_path)},
                        )
                        results.append(
                            (
                                "cleanup watched folder removed",
                                remove_folder.status_code == 200,
                                response_detail(remove_folder),
                            )
                        )
                    except Exception as exc:
                        results.append(("cleanup watched folder removed", False, repr(exc)))

                shutil.rmtree(temp_dir_path, ignore_errors=True)
                results.append(("cleanup temp directory deleted", not temp_dir_path.exists(), str(temp_dir_path)))
            elif test_file_path is not None and test_file_path.exists():
                os.unlink(test_file_path)
                results.append(("cleanup temp file deleted", not test_file_path.exists(), str(test_file_path)))

    print_results(results)
    return all(ok for _, ok, _ in results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="Valid Supabase JWT to test with.")
    parser.add_argument("--base-url", default=BASE, help=f"Backend base URL. Default: {BASE}")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--index-wait",
        type=float,
        default=DEFAULT_INDEX_WAIT_SECONDS,
        help=f"How long to wait for the temp file to become searchable. Default: {DEFAULT_INDEX_WAIT_SECONDS}",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    ok = asyncio.run(run(arguments))
    sys.exit(0 if ok else 1)
