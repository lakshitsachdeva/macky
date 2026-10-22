from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database
from backend.indexer import embedder
from backend.retrieval import query_parser, search as hybrid_search


BENCHMARK_CASES: list[tuple[str, str, str]] = [
    (
        "Grade IV syllabus",
        "last week i got syllabus for grade IV",
        "1759925171413.Gr 4 Term II Syllabus.pdf",
    ),
    (
        "Course policy",
        "cyber security course policy 2025 26",
        "Course Policy-Cyber Security 2025-26.pdf",
    ),
    (
        "Memex doc",
        "memex how it works problem core idea",
        "Memex_How_It_Works.docx",
    ),
    (
        "RAD chapter",
        "requirement analysis chapter 4",
        "Requirement Analysis & Design- chapter 4.pdf",
    ),
    (
        "IVP syllabus",
        "image and video processing syllabus",
        "Syllabus_ Image and Video Processing.pdf",
    ),
    (
        "Resume",
        "lakshit college resume btech",
        "resume Lakshit College.pdf",
    ),
    (
        "Marksheet",
        "latest marksheet lakshit",
        "Latest Marksheet Lakshit.pdf",
    ),
    (
        "Coding worksheet",
        "valid palindrome longest substring activity 2",
        "Activity 2 (pyhthon).pdf",
    ),
    (
        "Trading PDF",
        "msft trade stress test athair",
        "athair-MSFT-84.pdf",
    ),
    (
        "AIMA book",
        "artificial intelligence modern approach stuart russell",
        "Stuart J. Russel, Peter Norvig - Artificial Intelligence_ A Modern Approach (2009, Pearson Education Inc.) - libgen.li.pdf",
    ),
    (
        "Formula screenshot",
        "verify mantissa",
        "verify_mantissa.png",
    ),
]


def _dense_only(query: str) -> list[str]:
    parsed = query_parser.parse_query(query)
    dense_vec, _ = embedder.embed_query(parsed.semantic_text)

    conn = database.get_connection()
    try:
        faiss_rows = conn.execute(
            """
            SELECT faiss_id, item_id
            FROM embeddings
            WHERE embedding_type = 'dense'
              AND faiss_id IS NOT NULL
            """
        ).fetchall()
        faiss_id_map = {int(row["faiss_id"]): row["item_id"] for row in faiss_rows}

        item_scores: dict[str, float] = {}
        faiss_ids, dense_scores = embedder.get_text_index().search(dense_vec, k=40)
        for faiss_id, score in zip(faiss_ids, dense_scores):
            item_id = faiss_id_map.get(faiss_id)
            if not item_id:
                continue
            item_scores[item_id] = max(item_scores.get(item_id, 0.0), float(score))

        if not item_scores:
            return []

        placeholders = ", ".join("?" for _ in item_scores)
        rows = conn.execute(
            f"SELECT id, filename FROM items WHERE id IN ({placeholders}) AND is_excluded = 0",
            list(item_scores),
        ).fetchall()
        id_to_filename = {row["id"]: row["filename"] for row in rows}

        ordered_ids = [
            item_id
            for item_id, _ in sorted(
                item_scores.items(),
                key=lambda entry: entry[1],
                reverse=True,
            )
            if item_id in id_to_filename
        ]
        return [id_to_filename[item_id] for item_id in ordered_ids[:5]]
    finally:
        conn.close()


def _fts_queries(*texts: str) -> list[str]:
    ordered_queries: list[str] = []
    seen_queries: set[str] = set()

    for text in texts:
        tokens: list[str] = []
        seen_tokens: set[str] = set()
        for token in re.findall(r"[A-Za-z0-9]+", text or ""):
            normalized = token.casefold()
            if len(normalized) < 2:
                continue
            if normalized in hybrid_search.FTS_STOPWORDS:
                continue
            if normalized in seen_tokens:
                continue
            seen_tokens.add(normalized)
            tokens.append(normalized)

        if not tokens:
            continue

        for query in (" OR ".join(tokens[:12]), " ".join(tokens[:6])):
            if query and query not in seen_queries:
                seen_queries.add(query)
                ordered_queries.append(query)

    return ordered_queries


def _fts_only(query: str) -> list[str]:
    parsed = query_parser.parse_query(query)

    conn = database.get_connection()
    try:
        item_scores: dict[str, float] = {}
        for fts_query in _fts_queries(parsed.semantic_text, parsed.original):
            for row in database.search_fts(conn, fts_query, limit=20):
                item_scores[row["id"]] = max(
                    item_scores.get(row["id"], 0.0),
                    float(row["bm25_score"]),
                )

        if not item_scores:
            return []

        placeholders = ", ".join("?" for _ in item_scores)
        rows = conn.execute(
            f"SELECT id, filename FROM items WHERE id IN ({placeholders}) AND is_excluded = 0",
            list(item_scores),
        ).fetchall()
        id_to_filename = {row["id"]: row["filename"] for row in rows}

        ordered_ids = [
            item_id
            for item_id, _ in sorted(
                item_scores.items(),
                key=lambda entry: entry[1],
                reverse=True,
            )
            if item_id in id_to_filename
        ]
        return [id_to_filename[item_id] for item_id in ordered_ids[:5]]
    finally:
        conn.close()


async def _hybrid(query: str) -> list[str]:
    results = await hybrid_search.search(query, limit=5)
    return [result.item["filename"] for result in results]


async def main() -> int:
    embedder.get_text_index().load()

    methods = {
        "hybrid": _hybrid,
        "dense": _dense_only,
        "fts": _fts_only,
    }
    metrics = {name: {"top1": 0, "top3": 0} for name in methods}

    for label, query, expected in BENCHMARK_CASES:
        print(f"\n[{label}] {query}")
        print(f"Expected: {expected}")

        for name, runner in methods.items():
            if name == "hybrid":
                filenames = await runner(query)
            else:
                filenames = runner(query)

            top1 = bool(filenames and filenames[0] == expected)
            top3 = expected in filenames[:3]
            metrics[name]["top1"] += int(top1)
            metrics[name]["top3"] += int(top3)

            marker = "✅" if top1 else "•"
            print(f"  {marker} {name:<6} top3={filenames[:3]}")

    total = len(BENCHMARK_CASES)
    print("\nSummary")
    print("=" * 60)
    for name, scores in metrics.items():
        print(
            f"{name:<6} top1={scores['top1']}/{total} "
            f"top3={scores['top3']}/{total}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
