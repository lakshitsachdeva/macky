#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.cognition import reranker
from backend.indexer import embedder
from backend.retrieval import query_parser
from backend.retrieval import search as retrieval_search
from scripts.benchmark_real_retrieval import (
    REAL_LAPTOP_CASES,
    _rank,
    _source_type_violations,
)


GRAPH_CONTRIBUTION_TARGETS = {
    "ENTITY_LOOKUP": (0.60, 0.80),
    "PERSON_CENTRIC": (0.60, 0.80),
    "TEMPORAL_STATE": (0.50, 0.70),
    "TEMPORAL_RANGE": (0.00, 0.20),
    "SEMANTIC_TOPIC": (0.10, 0.30),
    "EXACT_LOOKUP": (0.00, 0.20),
}


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


def _graph_contribution(result: retrieval_search.SearchResult) -> bool:
    return str(result.source_pool) == "graph" or float(result.graph_score or 0.0) > 0.1


def _summarize_by_intent(entries: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        buckets[str(entry["intent_class"])].append(entry)

    summary: dict[str, Any] = {}
    for intent_class, rows in sorted(buckets.items()):
        count = len(rows)
        latencies = [float(row["latency_ms"]) for row in rows]
        top1 = sum(1 for row in rows if row["rank"] == 1)
        top3 = sum(1 for row in rows if row["rank"] is not None and row["rank"] <= 3)
        graph_contrib = sum(1 for row in rows if bool(row["graph_contributed"]))
        graph_only_wins = sum(int(row["graph_only_wins"]) for row in rows)
        target = GRAPH_CONTRIBUTION_TARGETS.get(intent_class)
        graph_rate = graph_contrib / max(1, count)
        summary[intent_class] = {
            "count": count,
            "top1_rate": round(top1 / max(1, count), 4),
            "top3_rate": round(top3 / max(1, count), 4),
            "graph_contribution_rate": round(graph_rate, 4),
            "graph_contribution_target": target,
            "graph_contribution_in_target": (
                bool(target and target[0] <= graph_rate <= target[1])
            ),
            "graph_only_wins": graph_only_wins,
            "p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
            "p95_ms": round(sorted(latencies)[min(count - 1, max(0, int(count * 0.95)))], 1) if latencies else 0.0,
            "avg_latency_delta_vs_base_ms": round(
                sum(float(row["latency_delta_vs_base_ms"]) for row in rows) / max(1, count),
                1,
            ),
        }
    return summary


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Intent Benchmark",
        "",
        f"- Created: `{report['created_at']}`",
        "",
        "## Intent Summary",
        "",
    ]
    for intent_class, summary in report["by_intent"].items():
        lines.extend(
            [
                f"### {intent_class}",
                "",
                f"- Cases: `{summary['count']}`",
                f"- Top-1: `{summary['top1_rate'] * 100:.1f}%`",
                f"- Top-3: `{summary['top3_rate'] * 100:.1f}%`",
                f"- Graph contribution: `{summary['graph_contribution_rate'] * 100:.1f}%` target `{summary['graph_contribution_target']}`",
                f"- Graph-only wins: `{summary['graph_only_wins']}`",
                f"- p50/p95: `{summary['p50_ms']}ms / {summary['p95_ms']}ms`",
                f"- Avg latency delta vs base-only: `{summary['avg_latency_delta_vs_base_ms']}ms`",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run intent-split retrieval benchmark for Recurdo")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "data" / "benchmarks"))
    return parser


async def async_main() -> int:
    args = build_arg_parser().parse_args()
    if not args.no_warmup:
        await embedder.initialize_model_pool()

    entries: list[dict[str, Any]] = []
    for case in REAL_LAPTOP_CASES:
        parsed = query_parser.parse_query(case.query)
        with _temporary_config(use_graph=False, use_dynamic_scoring=False):
            base_started = time.perf_counter()
            base_results = await retrieval_search.search(case.query, limit=max(10, int(args.limit)))
            base_latency_ms = (time.perf_counter() - base_started) * 1000.0
        with _temporary_config(use_graph=True, use_dynamic_scoring=True):
            started = time.perf_counter()
            results = await retrieval_search.search(case.query, limit=max(10, int(args.limit)))
            latency_ms = (time.perf_counter() - started) * 1000.0

        rank = _rank(results, case.expected_any)
        forbidden = _source_type_violations(results, case.forbid_source_types, top_k=min(int(args.limit), 5))
        graph_contributed = any(_graph_contribution(result) for result in results[:3])
        base_top10 = {str(result.item.get("id") or result.item_id) for result in base_results[:10]}
        graph_only_wins = 0
        for result in results[:3]:
            if _graph_contribution(result) and str(result.item.get("id") or result.item_id) not in base_top10:
                graph_only_wins += 1

        entry = {
            "query": case.query,
            "intent_class": parsed.intent_class,
            "rank": rank,
            "accepted": rank is not None and rank <= case.max_rank and not forbidden,
            "graph_contributed": graph_contributed,
            "graph_only_wins": graph_only_wins,
            "latency_ms": round(latency_ms, 1),
            "base_latency_ms": round(base_latency_ms, 1),
            "latency_delta_vs_base_ms": round(latency_ms - base_latency_ms, 1),
        }
        entries.append(entry)
        if args.verbose:
            print(
                f"{parsed.intent_class:15} {'PASS' if entry['accepted'] else 'FAIL':4} "
                f"graph={entry['graph_contributed']} wins={graph_only_wins} "
                f"delta={entry['latency_delta_vs_base_ms']:6.1f}ms | {case.query}"
            )

    report = {
        "suite": "intent_benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": entries,
        "by_intent": _summarize_by_intent(entries),
    }

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"intent_benchmark_{stamp}.json"
    md_path = out_dir / f"intent_benchmark_{stamp}.md"
    latest_json = out_dir / "intent_benchmark_latest.json"
    latest_md = out_dir / "intent_benchmark_latest.md"
    payload = json.dumps(report, indent=2, ensure_ascii=True)
    json_path.write_text(payload + "\n", encoding="utf-8")
    latest_json.write_text(payload + "\n", encoding="utf-8")
    _write_markdown(report, md_path)
    _write_markdown(report, latest_md)

    print()
    print("Intent benchmark")
    for intent_class, summary in report["by_intent"].items():
        print(
            f"  {intent_class:15} top1={summary['top1_rate'] * 100:5.1f}% "
            f"graph={summary['graph_contribution_rate'] * 100:5.1f}% "
            f"wins={summary['graph_only_wins']:2d} "
            f"delta={summary['avg_latency_delta_vs_base_ms']:6.1f}ms"
        )
    print(f"  report: {md_path}")
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
