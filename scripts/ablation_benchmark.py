#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.cognition import reranker
from backend.indexer import embedder
from backend.retrieval import search as retrieval_search
from scripts.benchmark_real_retrieval import (
    REAL_LAPTOP_CASES,
    _rank,
    _source_type_violations,
)


CONFIGS: tuple[tuple[str, dict[str, bool]], ...] = (
    ("base_only", {"use_graph": False, "use_dynamic_scoring": False}),
    ("base_plus_graph", {"use_graph": True, "use_dynamic_scoring": False}),
    ("base_plus_dynamic", {"use_graph": False, "use_dynamic_scoring": True}),
    ("full_system", {"use_graph": True, "use_dynamic_scoring": True}),
)


@contextmanager
def _temporary_config(*, use_graph: bool, use_dynamic_scoring: bool) -> Iterator[None]:
    old_search_graph = retrieval_search.USE_GRAPH_CANDIDATES
    old_search_dynamic = retrieval_search.USE_DYNAMIC_SCORING
    old_rerank_graph = reranker.USE_GRAPH_RERANK
    old_rerank_dynamic = reranker.USE_DYNAMIC_GRAPH_WEIGHTING
    retrieval_search.USE_GRAPH_CANDIDATES = bool(use_graph)
    retrieval_search.USE_DYNAMIC_SCORING = bool(use_dynamic_scoring)
    reranker.USE_GRAPH_RERANK = bool(use_graph)
    reranker.USE_DYNAMIC_GRAPH_WEIGHTING = bool(use_dynamic_scoring)
    try:
        yield
    finally:
        retrieval_search.USE_GRAPH_CANDIDATES = old_search_graph
        retrieval_search.USE_DYNAMIC_SCORING = old_search_dynamic
        reranker.USE_GRAPH_RERANK = old_rerank_graph
        reranker.USE_DYNAMIC_GRAPH_WEIGHTING = old_rerank_dynamic


def _summarize(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(case_results)
    latencies = [float(entry["latency_ms"]) for entry in case_results]
    top1 = sum(1 for entry in case_results if entry["rank"] == 1)
    top3 = sum(1 for entry in case_results if entry["rank"] is not None and entry["rank"] <= 3)
    return {
        "count": count,
        "top1": top1,
        "top1_rate": round(top1 / max(1, count), 4),
        "top3": top3,
        "top3_rate": round(top3 / max(1, count), 4),
        "accepted": sum(1 for entry in case_results if entry["accepted"]),
        "accepted_rate": round(sum(1 for entry in case_results if entry["accepted"]) / max(1, count), 4),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_ms": round(sorted(latencies)[min(count - 1, max(0, int(count * 0.95)))], 1) if latencies else 0.0,
    }


async def _run_config(
    name: str,
    *,
    limit: int,
    use_graph: bool,
    use_dynamic_scoring: bool,
    verbose: bool,
) -> dict[str, Any]:
    case_results: list[dict[str, Any]] = []
    with _temporary_config(use_graph=use_graph, use_dynamic_scoring=use_dynamic_scoring):
        for case in REAL_LAPTOP_CASES:
            started = time.perf_counter()
            results = await retrieval_search.search(case.query, limit=limit)
            latency_ms = (time.perf_counter() - started) * 1000.0
            rank = _rank(results, case.expected_any)
            forbidden = _source_type_violations(results, case.forbid_source_types, top_k=min(limit, 5))
            accepted = rank is not None and rank <= case.max_rank and not forbidden
            entry = {
                "query": case.query,
                "intent_class": retrieval_search.query_parser.parse_query(case.query).intent_class,
                "rank": rank,
                "accepted": accepted,
                "latency_ms": round(latency_ms, 1),
                "top_result": str(results[0].item.get("filename") or results[0].item.get("title") or "") if results else "",
            }
            case_results.append(entry)
            if verbose:
                print(
                    f"{name:17} {'PASS' if accepted else 'FAIL':4} "
                    f"rank={str(rank or '-'):>2} {latency_ms:7.1f}ms | {case.query}"
                )

    return {
        "name": name,
        "flags": {
            "use_graph": use_graph,
            "use_dynamic_scoring": use_dynamic_scoring,
        },
        "summary": _summarize(case_results),
        "cases": case_results,
    }


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Retrieval Ablation Benchmark",
        "",
        f"- Created: `{report['created_at']}`",
        f"- Cases: `{len(REAL_LAPTOP_CASES)}`",
        "",
        "## Configs",
        "",
    ]
    for config in report["configs"]:
        summary = config["summary"]
        lines.extend(
            [
                f"### {config['name']}",
                "",
                f"- Flags: `{json.dumps(config['flags'], ensure_ascii=True)}`",
                f"- Accepted: `{summary['accepted']}/{summary['count']}` (`{summary['accepted_rate'] * 100:.1f}%`)",
                f"- Top-1: `{summary['top1']}/{summary['count']}` (`{summary['top1_rate'] * 100:.1f}%`)",
                f"- Top-3: `{summary['top3']}/{summary['count']}` (`{summary['top3_rate'] * 100:.1f}%`)",
                f"- p50/p95: `{summary['p50_ms']}ms / {summary['p95_ms']}ms`",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run retrieval ablation benchmarks for Recurdo")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "data" / "benchmarks"))
    return parser


async def async_main() -> int:
    args = build_arg_parser().parse_args()
    if not args.no_warmup:
        await embedder.initialize_model_pool()

    configs = []
    for name, flags in CONFIGS:
        configs.append(
            await _run_config(
                name,
                limit=max(1, int(args.limit)),
                verbose=bool(args.verbose),
                **flags,
            )
        )

    report = {
        "suite": "ablation_benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "configs": configs,
    }

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"ablation_benchmark_{stamp}.json"
    md_path = out_dir / f"ablation_benchmark_{stamp}.md"
    latest_json = out_dir / "ablation_benchmark_latest.json"
    latest_md = out_dir / "ablation_benchmark_latest.md"
    payload = json.dumps(report, indent=2, ensure_ascii=True)
    json_path.write_text(payload + "\n", encoding="utf-8")
    latest_json.write_text(payload + "\n", encoding="utf-8")
    _write_markdown(report, md_path)
    _write_markdown(report, latest_md)

    print()
    print("Retrieval ablation benchmark")
    for config in configs:
        summary = config["summary"]
        print(
            f"  {config['name']:17} top1={summary['top1_rate'] * 100:5.1f}% "
            f"top3={summary['top3_rate'] * 100:5.1f}% "
            f"p50/p95={summary['p50_ms']:6.1f}/{summary['p95_ms']:6.1f}ms"
        )
    print(f"  report: {md_path}")
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
