#!/usr/bin/env python3
"""
Recurdo real-corpus search benchmark.

Uses real queries from Lakshit's current library: Recurdo docs, college notes,
research drafts, spreadsheets, and a few image lookups. The cases are intended
to stay stable across ranking changes unless the retrieval quality is improved
deliberately.

Examples:
  python backend/benchmark_real.py --verbose
  python backend/benchmark_real.py --token YOUR_TOKEN --base http://127.0.0.1:8000
  python backend/benchmark_real.py --save-baseline --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:8000"
DEFAULT_SESSION_PATH = Path.home() / ".recurdo" / "session.json"
DEFAULT_BASELINE_PATH = Path(__file__).with_name("benchmark_real_baseline.json")

# Format: (query, expected_filename_substring, description)
BENCHMARK_CASES: list[tuple[str, str, str]] = [
    # Recurdo product / project docs
    ("recurdo bible", "recurdo bible", "exact product bible lookup"),
    ("production readiness", "production readiness", "release readiness plan"),
    ("indexing speed audit", "indexing speed audit", "indexing audit notes"),
    ("indexing speed fixes", "indexing speed fixes", "fix follow-up notes"),
    ("elevator pitch guide", "elevator pitch guide", "messaging / GTM doc"),
    ("complete deep dive", "complete deep dive", "deep technical/product overview"),
    ("memex how it works", "memex how it works", "design/architecture doc"),
    ("personal memory blueprint", "personal memory engine blueprint", "product blueprint"),

    # Research / applications / personal docs
    ("proposal iitb pdf", "proposal iitb", "proposal exact pdf lookup"),
    ("proposal iitb docx", "proposal iitb", "proposal editable doc lookup"),
    ("arithretrieval proposal", "arithretrieval proposal", "specific research proposal"),
    ("lakshit resume", "lakshit resume", "resume lookup"),
    ("iima application", "iima application", "application doc"),
    ("outreach strategy", "outreach strategy", "outreach planning doc"),
    ("email iitb", "email iitb", "draft mail lookup"),
    ("autodesk backup code", "autodesk backup code", "credential/code note"),

    # College / coursework
    ("interior design project 2", "interior design project2", "course project document"),
    ("sequence of submission docx", "sequence of submission", "exact admin doc lookup"),
    ("program security pdf", "program security pdf", "prefer pdf over slides"),
    ("program security slides", "program security pptx", "prefer presentation over pdf"),
    ("access control pdf", "access control pdf", "prefer pdf over slides"),
    ("access control slides", "access control pptx", "prefer presentation over pdf"),
    ("mid term test 2 interior design pdf", "mid term test 2", "interior design notes"),
    ("dns ids project guide", "dns ids project guide", "project guide exact lookup"),
    ("dns ids codex prompts", "dns ids codex prompts", "prompt pack exact lookup"),
    ("singapore malaysia expense xlsx", "singapore malaysia trip expense", "travel spreadsheet"),

    # Spreadsheet-heavy cases
    ("trip settlement master", "trip settlement master", "settlement spreadsheet"),
    ("peer review log", "peer review log", "exact spreadsheet log lookup"),
    ("bug report spreadsheet", "bug report", "bug-report spreadsheet lookup"),
    ("gate mega sheet", "gate mega sheet", "important exam spreadsheet"),
    ("risk monitoring log", "risk monitoring log", "risk log spreadsheet"),

    # Visual / scanned / mixed content
    ("sunlight living dining room", "sunlight space living and dining room", "image title/visual lookup"),
    ("wabi sabi living dining room", "wabi sabi living dining room", "image style lookup"),
    ("adobe scan removed", "adobe scan 18 mar 2026 removed", "scanned pdf variant"),
    ("ie delhi 22 march", "ie delhi 22 03 2026", "date-heavy pdf lookup"),
    ("athair complete bible", "athair complete bible", "brand bible lookup"),
    ("linear regression slides", "linear regression", "presentation lookup"),
]


def normalize_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def filename_matches_expected(filename: str, expected: str) -> bool:
    filename = filename or ""
    expected = expected or ""
    if not expected:
        return bool(filename)

    filename_lower = filename.casefold()
    expected_lower = expected.casefold().strip()

    if expected_lower in filename_lower:
        return True

    if normalize_for_match(expected_lower) in normalize_for_match(filename_lower):
        return True

    expected_tokens = [token for token in re.findall(r"[a-z0-9]+", expected_lower) if len(token) > 1]
    filename_tokens = [token for token in re.findall(r"[a-z0-9]+", filename_lower) if len(token) > 1]
    if not expected_tokens or not filename_tokens:
        return False

    matched = 0
    for expected_token in expected_tokens:
        if any(
            expected_token == filename_token
            or expected_token in filename_token
            or filename_token in expected_token
            for filename_token in filename_tokens
        ):
            matched += 1

    return matched == len(expected_tokens)


def detect_running_backend_base() -> str | None:
    pid_result = subprocess.run(
        "pgrep -f recurdo-backend | head -n1",
        shell=True,
        capture_output=True,
        text=True,
    )
    pid = pid_result.stdout.strip()
    if not pid:
        return None

    env_result = subprocess.run(
        f"ps eww -p {pid}",
        shell=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"RECURDO_PORT=(\d+)", env_result.stdout)
    if not match:
        return None

    return f"http://127.0.0.1:{match.group(1)}"


def resolve_base(base_arg: str | None) -> str:
    if base_arg:
        return base_arg.rstrip("/")

    detected = detect_running_backend_base()
    if detected:
        return detected

    return DEFAULT_BASE


def load_token(session_path: Path, token_arg: str | None) -> str:
    if token_arg:
        return token_arg.strip()

    if not session_path.exists():
        raise SystemExit(
            f"Session file not found at {session_path}. Pass --token explicitly."
        )

    payload = json.loads(session_path.read_text())
    token = str(payload.get("token") or "").strip()
    if not token:
        raise SystemExit(
            f"No token found in {session_path}. Pass --token explicitly."
        )
    return token


async def fetch_results(
    client: httpx.AsyncClient,
    *,
    base: str,
    token: str,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    response = await client.post(
        f"{base}/search",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "limit": limit},
    )

    if response.status_code == 401:
        raise RuntimeError("INVALID_TOKEN: session expired or token rejected")

    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected /search response shape: {type(payload).__name__}")

    return payload


def summarize(results: list[dict[str, Any]]) -> dict[str, float]:
    latencies = [float(item["latency_ms"]) for item in results]
    total = len(results)
    p1 = sum(1 for item in results if item["top1"])
    p3 = sum(1 for item in results if item["top3"])

    return {
        "count": total,
        "top1_hits": p1,
        "top3_hits": p3,
        "top1_rate": (p1 / total) if total else 0.0,
        "top3_rate": (p3 / total) if total else 0.0,
        "p50_ms": statistics.median(latencies) if latencies else 0.0,
        "p95_ms": sorted(latencies)[min(total - 1, max(0, int(total * 0.95)))] if latencies else 0.0,
    }


def compare_results(
    current_results: list[dict[str, Any]],
    baseline_payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not baseline_payload:
        return [], []

    baseline_by_query = {
        item["query"]: item
        for item in baseline_payload.get("results", [])
        if isinstance(item, dict) and item.get("query")
    }

    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []

    for current in current_results:
        baseline = baseline_by_query.get(current["query"])
        if not baseline:
            continue

        baseline_top1 = bool(baseline.get("top1"))
        baseline_top3 = bool(baseline.get("top3"))
        current_top1 = bool(current["top1"])
        current_top3 = bool(current["top3"])

        if (baseline_top1 and not current_top1) or (baseline_top3 and not current_top3):
            regressions.append(
                {
                    "query": current["query"],
                    "baseline_top1": baseline_top1,
                    "baseline_top3": baseline_top3,
                    "current_top1": current_top1,
                    "current_top3": current_top3,
                    "baseline_top_result": baseline.get("top_result", ""),
                    "current_top_result": current.get("top_result", ""),
                }
            )
        elif (not baseline_top1 and current_top1) or (not baseline_top3 and current_top3):
            improvements.append(
                {
                    "query": current["query"],
                    "baseline_top1": baseline_top1,
                    "baseline_top3": baseline_top3,
                    "current_top1": current_top1,
                    "current_top3": current_top3,
                    "baseline_top_result": baseline.get("top_result", ""),
                    "current_top_result": current.get("top_result", ""),
                }
            )

    return regressions, improvements


def save_baseline(
    path: Path,
    *,
    base: str,
    results: list[dict[str, Any]],
    summary: dict[str, float],
) -> None:
    payload = {
        "base": base,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": summary,
        "results": results,
    }
    path.write_text(json.dumps(payload, indent=2))


async def run_benchmark(
    *,
    token: str,
    base: str,
    limit: int,
    verbose: bool,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for query, expected, description in BENCHMARK_CASES:
            t0 = time.perf_counter()
            hits = await fetch_results(
                client,
                base=base,
                token=token,
                query=query,
                limit=limit,
            )
            latency_ms = (time.perf_counter() - t0) * 1000

            filenames = [str(item.get("filename", "")) for item in hits]
            top1 = bool(filenames) and filename_matches_expected(filenames[0], expected)
            top3 = any(filename_matches_expected(filename, expected) for filename in filenames[:3])

            result = {
                "query": query,
                "expected": expected,
                "description": description,
                "top1": top1,
                "top3": top3,
                "latency_ms": round(latency_ms, 1),
                "top_result": filenames[0] if filenames else "(no results)",
                "top3_results": filenames[:3],
            }
            results.append(result)

            if verbose:
                icon = "✓" if top1 else ("~" if top3 else "✗")
                print(
                    f"  {icon} [{latency_ms:5.0f}ms] {query[:42]:<42} "
                    f"→ {result['top_result']}"
                )

    return results, summarize(results)


def print_summary(
    *,
    base: str,
    summary: dict[str, float],
    results: list[dict[str, Any]],
    regressions: list[dict[str, Any]],
    improvements: list[dict[str, Any]],
) -> None:
    n = int(summary["count"])
    print(f"\n{'═' * 68}")
    print("  REAL-CORPUS BENCHMARK")
    print(f"  base:  {base}")
    print(
        f"  top-1: {int(summary['top1_hits'])}/{n} "
        f"({summary['top1_rate'] * 100:.0f}%)"
    )
    print(
        f"  top-3: {int(summary['top3_hits'])}/{n} "
        f"({summary['top3_rate'] * 100:.0f}%)"
    )
    print(
        f"  p50={summary['p50_ms']:.0f}ms  "
        f"p95={summary['p95_ms']:.0f}ms"
    )

    misses = [item for item in results if not item["top1"]]
    if misses:
        print("\n  MISSES (not in top-1):")
        for miss in misses:
            icon = "~" if miss["top3"] else "✗"
            print(
                f"  {icon} '{miss['query']}' → got '{miss['top_result']}' "
                f"(expected '{miss['expected']}')"
            )

    if regressions:
        print("\n  REGRESSIONS vs baseline:")
        for item in regressions:
            print(
                f"  ✗ '{item['query']}' "
                f"top1 {item['baseline_top1']}→{item['current_top1']} "
                f"top3 {item['baseline_top3']}→{item['current_top3']} "
                f"('{item['baseline_top_result']}' → '{item['current_top_result']}')"
            )

    if improvements:
        print("\n  IMPROVEMENTS vs baseline:")
        for item in improvements:
            print(
                f"  ✓ '{item['query']}' "
                f"top1 {item['baseline_top1']}→{item['current_top1']} "
                f"top3 {item['baseline_top3']}→{item['current_top3']} "
                f"('{item['baseline_top_result']}' → '{item['current_top_result']}')"
            )

    print(f"{'═' * 68}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run Recurdo real-corpus search benchmark")
    parser.add_argument("--token", help="Bearer token for the backend")
    parser.add_argument(
        "--session-path",
        default=str(DEFAULT_SESSION_PATH),
        help=f"Session JSON path (default: {DEFAULT_SESSION_PATH})",
    )
    parser.add_argument(
        "--base",
        help="Backend base URL. Defaults to the running packaged backend if detected, else http://127.0.0.1:8000",
    )
    parser.add_argument("--limit", type=int, default=5, help="Search result limit per query")
    parser.add_argument("--verbose", action="store_true", help="Print one-line status for every query")
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE_PATH),
        help=f"Baseline JSON path (default: {DEFAULT_BASELINE_PATH})",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Write the current run to the baseline JSON path",
    )
    parser.add_argument(
        "--no-compare",
        action="store_true",
        help="Skip regression comparison against the baseline file",
    )
    args = parser.parse_args()

    session_path = Path(args.session_path).expanduser()
    baseline_path = Path(args.baseline).expanduser()
    base = resolve_base(args.base)
    token = load_token(session_path, args.token)

    try:
        results, summary = await run_benchmark(
            token=token,
            base=base,
            limit=args.limit,
            verbose=args.verbose,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    baseline_payload: dict[str, Any] | None = None
    if not args.no_compare and baseline_path.exists():
        try:
            baseline_payload = json.loads(baseline_path.read_text())
        except json.JSONDecodeError:
            baseline_payload = None

    regressions, improvements = compare_results(results, baseline_payload)
    print_summary(
        base=base,
        summary=summary,
        results=results,
        regressions=regressions,
        improvements=improvements,
    )

    if args.save_baseline:
        save_baseline(
            baseline_path,
            base=base,
            results=results,
            summary=summary,
        )
        print(f"\nSaved baseline to {baseline_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
