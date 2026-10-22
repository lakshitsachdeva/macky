#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.indexer import embedder
from backend.retrieval.search import SearchResult, search


@dataclass(frozen=True)
class BenchmarkCase:
    query: str
    expected_any: tuple[str, ...]
    category: str
    description: str
    max_rank: int = 1
    forbid_source_types: tuple[str, ...] = ()


REAL_LAPTOP_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        query="athair complete bible",
        expected_any=("athair_complete_bible.md",),
        category="exact_doc",
        description="Athair engineering/product bible",
    ),
    BenchmarkCase(
        query="recurdo bible",
        expected_any=("recurdo_bible.pdf",),
        category="exact_doc",
        description="Recurdo product bible",
    ),
    BenchmarkCase(
        query="recurdo complete deep dive",
        expected_any=("recurdo_complete_deep_dive", "c40a9bd0"),
        category="exact_doc",
        description="Recurdo complete deep dive PDF",
    ),
    BenchmarkCase(
        query="athair landing page codex implementation prompt",
        expected_any=("athair-codex-prompt.md",),
        category="exact_doc",
        description="Athair landing page implementation prompt",
    ),
    BenchmarkCase(
        query="swarm alpha development journal",
        expected_any=("swarm_alpha_development_journal", "dev journal.pdf"),
        category="exact_doc",
        description="Swarm Alpha development journal",
    ),
    BenchmarkCase(
        query="athair complete master plan",
        expected_any=("athair_master_plan.md",),
        category="exact_doc",
        description="Athair master plan",
    ),
    BenchmarkCase(
        query="fastapi 0.115.0 requirements",
        expected_any=("requirements.txt",),
        category="exact_doc",
        description="Backend requirements lookup",
    ),
    BenchmarkCase(
        query="adaptive study project report",
        expected_any=(
            "adaptivestudy_project_report.docx",
            "adaptivestudy_hci_report.docx",
            "case_study_b102_b103.pdf",
        ),
        category="project_doc",
        description="AdaptiveStudy report cluster",
        max_rank=3,
    ),
    BenchmarkCase(
        query="statement of purpose lakshit",
        expected_any=("statement of purpose.docx",),
        category="personal_doc",
        description="Statement of purpose",
    ),
    BenchmarkCase(
        query="research internship resume lakshit iitg",
        expected_any=("resume_lakshit_iitg.pdf", "resume lakshit.pdf", "lakshit_resume_final"),
        category="personal_doc",
        description="Resume lookup",
        max_rank=3,
    ),
    BenchmarkCase(
        query="uom professor ben rubinstein",
        expected_any=("prof ben rubinstein",),
        category="research_outreach",
        description="University of Melbourne professor PDF",
        max_rank=3,
    ),
    BenchmarkCase(
        query="professor uom mailing thing",
        expected_any=("prof ben rubinstein", "prof trevor cohn", "prof eduardo velloso", "img_8664"),
        category="research_outreach",
        description="Fuzzy UOM mailing memory",
        max_rank=5,
    ),
    BenchmarkCase(
        query="risk monitoring log practical4",
        expected_any=("risk_monitoring_log_practical4.xlsx", "risk_monitoring_log"),
        category="spreadsheet",
        description="Risk monitoring spreadsheet",
    ),
    BenchmarkCase(
        query="unit 5 program security pdf",
        expected_any=("unit-5-program security.pdf",),
        category="coursework",
        description="Program security PDF",
    ),
    BenchmarkCase(
        query="linear regression slides",
        expected_any=("linear regression.pptx", "linear regression"),
        category="coursework",
        description="Linear regression presentation",
        max_rank=3,
    ),
    BenchmarkCase(
        query="sunlight living dining room",
        expected_any=("sunlight space_living and dining room",),
        category="visual",
        description="Interior image visual lookup",
        max_rank=3,
    ),
    BenchmarkCase(
        query="wabi sabi living dining room",
        expected_any=("wabi-sabi style living and dining room", "wabi sabi living dining"),
        category="visual",
        description="Wabi-sabi interior image lookup",
        max_rank=3,
    ),
    BenchmarkCase(
        query="email iitb",
        expected_any=("email_iitb.txt",),
        category="draft",
        description="IITB draft email",
    ),
    BenchmarkCase(
        query="the stocks related project i was working on in february",
        expected_any=(
            "athair",
            "swarm_alpha",
            "rate_limits_and_accuracy",
            "system_workflow",
            "codebase_deep_dive",
        ),
        category="temporal_project",
        description="Broad February finance/startup project query",
        max_rank=3,
        forbid_source_types=("gmail", "gmail_attachment"),
    ),
    BenchmarkCase(
        query="what major finance project or startup was i working on in february",
        expected_any=(
            "athair",
            "swarm_alpha",
            "rate_limits_and_accuracy",
            "system_workflow",
            "codebase_deep_dive",
        ),
        category="temporal_project",
        description="Broad February project recall query",
        max_rank=5,
        forbid_source_types=("gmail", "gmail_attachment"),
    ),
    BenchmarkCase(
        query="what was i working on in february for recurdo",
        expected_any=(
            "recurdo_bible",
            "recurdo_planz",
            "recurdo_cognition_spec",
            "recurdo_masterplan",
            "recurdo_complete_deep_dive",
            "recurdo_strategic_crossroads",
            "athair",
            "swarm_alpha",
        ),
        category="temporal_project",
        description="Broad February Recurdo recall query",
        max_rank=5,
        forbid_source_types=("gmail", "gmail_attachment"),
    ),
)


def _normalize(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("_", " ").replace("-", " ").split())


def _compact(value: str) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _result_text(result: SearchResult) -> str:
    item = result.item
    return " ".join(
        str(item.get(field) or "")
        for field in ("filename", "title", "original_path", "summary", "preview_text")
    )


def _matches(result: SearchResult, expected_any: tuple[str, ...]) -> bool:
    haystack = _normalize(_result_text(result))
    compact_haystack = _compact(_result_text(result))
    for expected in expected_any:
        normalized_expected = _normalize(expected)
        if normalized_expected and normalized_expected in haystack:
            return True
        compact_expected = _compact(expected)
        if compact_expected and compact_expected in compact_haystack:
            return True
    return False


def _rank(results: list[SearchResult], expected_any: tuple[str, ...]) -> int | None:
    for index, result in enumerate(results, start=1):
        if _matches(result, expected_any):
            return index
    return None


def _source_type_violations(results: list[SearchResult], forbidden: tuple[str, ...], *, top_k: int) -> list[str]:
    if not forbidden:
        return []
    forbidden_set = {source.casefold() for source in forbidden}
    violations: list[str] = []
    for result in results[:top_k]:
        source_type = str(result.item.get("source_type") or "").casefold()
        if source_type in forbidden_set:
            violations.append(str(result.item.get("filename") or result.item.get("title") or result.item_id))
    return violations


def _summarize(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(case_results)
    latencies = [float(result["latency_ms"]) for result in case_results]
    top1 = sum(1 for result in case_results if result["rank"] == 1)
    top3 = sum(1 for result in case_results if result["rank"] is not None and result["rank"] <= 3)
    accepted = sum(1 for result in case_results if result["accepted"])
    mrr = sum((1.0 / result["rank"]) if result["rank"] else 0.0 for result in case_results) / max(1, total)
    ndcg = sum((1.0 / math.log2(result["rank"] + 1)) if result["rank"] else 0.0 for result in case_results) / max(1, total)
    category_summary: dict[str, dict[str, Any]] = {}
    for result in case_results:
        category = str(result["category"])
        bucket = category_summary.setdefault(category, {"count": 0, "accepted": 0, "top1": 0, "top3": 0})
        bucket["count"] += 1
        bucket["accepted"] += int(bool(result["accepted"]))
        bucket["top1"] += int(result["rank"] == 1)
        bucket["top3"] += int(result["rank"] is not None and result["rank"] <= 3)
    for bucket in category_summary.values():
        count = max(1, int(bucket["count"]))
        bucket["accepted_rate"] = round(float(bucket["accepted"]) / count, 4)
        bucket["top1_rate"] = round(float(bucket["top1"]) / count, 4)
        bucket["top3_rate"] = round(float(bucket["top3"]) / count, 4)
    return {
        "count": total,
        "accepted": accepted,
        "accepted_rate": round(accepted / max(1, total), 4),
        "top1": top1,
        "top1_rate": round(top1 / max(1, total), 4),
        "top3": top3,
        "top3_rate": round(top3 / max(1, total), 4),
        "mrr": round(mrr, 4),
        "ndcg": round(ndcg, 4),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_ms": round(sorted(latencies)[min(total - 1, max(0, int(total * 0.95)))], 1) if latencies else 0.0,
        "categories": category_summary,
    }


async def run_cases(*, limit: int, warm_models: bool, verbose: bool) -> dict[str, Any]:
    if warm_models:
        await embedder.initialize_model_pool()

    case_results: list[dict[str, Any]] = []
    for case in REAL_LAPTOP_CASES:
        started = time.perf_counter()
        results = await search(case.query, limit=limit)
        latency_ms = (time.perf_counter() - started) * 1000
        rank = _rank(results, case.expected_any)
        forbidden = _source_type_violations(results, case.forbid_source_types, top_k=min(limit, 5))
        accepted = rank is not None and rank <= case.max_rank and not forbidden
        top_results = [
            {
                "rank": index,
                "filename": str(result.item.get("filename") or ""),
                "title": str(result.item.get("title") or ""),
                "source_type": str(result.item.get("source_type") or ""),
                "score": round(float(result.final_score), 4),
                "frame_type": result.frame_type,
                "matched_fields": list(result.matched_fields),
            }
            for index, result in enumerate(results[: min(limit, 8)], start=1)
        ]
        result_payload = {
            **asdict(case),
            "expected_any": list(case.expected_any),
            "forbid_source_types": list(case.forbid_source_types),
            "rank": rank,
            "accepted": accepted,
            "forbidden_hits": forbidden,
            "latency_ms": round(latency_ms, 1),
            "top_results": top_results,
        }
        case_results.append(result_payload)
        if verbose:
            marker = "PASS" if accepted else "FAIL"
            top = top_results[0]["filename"] if top_results else "(none)"
            print(f"{marker:4} rank={str(rank or '-'):>2} {latency_ms:7.1f}ms | {case.query} -> {top}")

    return {
        "suite": "real_laptop_retrieval",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "model_pool": embedder.get_model_pool_status(),
        "summary": _summarize(case_results),
        "cases": case_results,
    }


def _write_markdown_report(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    lines = [
        "# Real Retrieval Benchmark",
        "",
        f"- Created: `{report['created_at']}`",
        f"- Cases: `{summary['count']}`",
        f"- Accepted: `{summary['accepted']}/{summary['count']}` (`{summary['accepted_rate'] * 100:.1f}%`)",
        f"- Top-1: `{summary['top1']}/{summary['count']}` (`{summary['top1_rate'] * 100:.1f}%`)",
        f"- Top-3: `{summary['top3']}/{summary['count']}` (`{summary['top3_rate'] * 100:.1f}%`)",
        f"- MRR: `{summary['mrr']}`",
        f"- nDCG: `{summary['ndcg']}`",
        f"- p50 latency: `{summary['p50_ms']}ms`",
        f"- p95 latency: `{summary['p95_ms']}ms`",
        "",
        "## Misses",
        "",
    ]
    misses = [case for case in report["cases"] if not case["accepted"]]
    if not misses:
        lines.append("- None")
    else:
        for case in misses:
            top = case["top_results"][0]["filename"] if case["top_results"] else "(none)"
            lines.append(
                f"- `{case['query']}` expected `{case['expected_any']}` within rank `{case['max_rank']}`, "
                f"got rank `{case['rank']}` top `{top}`"
            )
    lines.extend(["", "## Cases", ""])
    for case in report["cases"]:
        marker = "PASS" if case["accepted"] else "FAIL"
        top = case["top_results"][0]["filename"] if case["top_results"] else "(none)"
        lines.append(
            f"- `{marker}` `{case['query']}` -> rank `{case['rank']}`, top `{top}`"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run real Recurdo retrieval benchmarks against the local laptop library")
    parser.add_argument("--limit", type=int, default=8, help="Search results to inspect per query")
    parser.add_argument("--no-warmup", action="store_true", help="Skip embedding model pool warmup")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "data" / "benchmarks"),
        help="Directory for JSON and Markdown benchmark reports",
    )
    parser.add_argument("--fail-under-accepted", type=float, default=0.90)
    parser.add_argument("--fail-under-top3", type=float, default=0.85)
    parser.add_argument("--fail-under-top1", type=float, default=0.55)
    return parser


async def async_main() -> int:
    args = build_arg_parser().parse_args()
    report = await run_cases(limit=max(1, int(args.limit)), warm_models=not args.no_warmup, verbose=bool(args.verbose))
    summary = report["summary"]

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"real_retrieval_benchmark_{stamp}.json"
    md_path = out_dir / f"real_retrieval_benchmark_{stamp}.md"
    latest_json = out_dir / "real_retrieval_benchmark_latest.json"
    latest_md = out_dir / "real_retrieval_benchmark_latest.md"
    json_payload = json.dumps(report, indent=2, ensure_ascii=True)
    json_path.write_text(json_payload + "\n", encoding="utf-8")
    latest_json.write_text(json_payload + "\n", encoding="utf-8")
    _write_markdown_report(report, md_path)
    _write_markdown_report(report, latest_md)

    print()
    print("Real retrieval benchmark")
    print(f"  accepted: {summary['accepted']}/{summary['count']} ({summary['accepted_rate'] * 100:.1f}%)")
    print(f"  top-1:    {summary['top1']}/{summary['count']} ({summary['top1_rate'] * 100:.1f}%)")
    print(f"  top-3:    {summary['top3']}/{summary['count']} ({summary['top3_rate'] * 100:.1f}%)")
    print(f"  mrr:      {summary['mrr']}")
    print(f"  ndcg:     {summary['ndcg']}")
    print(f"  p50/p95:  {summary['p50_ms']}ms / {summary['p95_ms']}ms")
    print(f"  report:   {md_path}")

    failed_thresholds = []
    if float(summary["accepted_rate"]) < float(args.fail_under_accepted):
        failed_thresholds.append("accepted")
    if float(summary["top3_rate"]) < float(args.fail_under_top3):
        failed_thresholds.append("top3")
    if float(summary["top1_rate"]) < float(args.fail_under_top1):
        failed_thresholds.append("top1")
    if failed_thresholds:
        print(f"  failed thresholds: {', '.join(failed_thresholds)}")
        return 1
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
