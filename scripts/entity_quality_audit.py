#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import config
from scripts.clean_entities import _connect, _load_entity_rows, collect_cleanup_candidates


def _print_top(rows: list[dict[str, Any]], *, title: str, limit: int = 20) -> None:
    print(f"\n{title} ({len(rows)}):")
    for row in rows[:limit]:
        print(
            f"  '{row['name']}' ({row['entity_type']}) "
            f"mentions={row['mention_edges']} facts={row['fact_count']}"
        )


def run_audit(graph_db: str | None = None) -> dict[str, Any]:
    graph_path = Path(graph_db or config.GRAPH_DB_PATH).expanduser()
    with _connect(graph_path) as conn:
        all_rows = _load_entity_rows(conn)
        candidates, reason_counts = collect_cleanup_candidates(conn)

    total = len(all_rows)
    noisy_by_reason: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        noisy_by_reason.setdefault(str(row["delete_reason"]), []).append(row)

    print("=" * 64)
    print("ENTITY QUALITY AUDIT")
    print("=" * 64)
    print(f"Graph DB: {graph_path}")
    print(f"Total entities: {total}")
    print(f"Estimated junk entities: {len(candidates)} ({(len(candidates)/max(1,total))*100:.1f}%)")
    print()
    print("Junk reason counts:")
    for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  - {reason}: {count}")

    for reason, rows in sorted(noisy_by_reason.items(), key=lambda item: (-len(item[1]), item[0])):
        _print_top(
            sorted(
                rows,
                key=lambda row: (int(row.get("mention_edges") or 0), int(row.get("fact_count") or 0)),
                reverse=True,
            ),
            title=f"Top {reason}",
        )

    low_length_rows = [row for row in all_rows if len(str(row.get("name") or "").strip()) <= 3]
    _print_top(
        sorted(
            low_length_rows,
            key=lambda row: (int(row.get("mention_edges") or 0), int(row.get("fact_count") or 0)),
            reverse=True,
        ),
        title="Top <=3 character entities",
    )

    type_counter = Counter(str(row.get("entity_type") or "Unknown") for row in all_rows)
    print("\nEntity type distribution:")
    for entity_type, count in type_counter.most_common(12):
        print(f"  - {entity_type}: {count}")

    estimated_mentions_removed = sum(int(row.get("mention_edges") or 0) for row in candidates)
    estimated_facts_removed = sum(int(row.get("fact_count") or 0) for row in candidates)
    print("\nEstimated impact if cleanup is executed:")
    print(f"  - entities removed: {len(candidates)}")
    print(f"  - mention edges removed: {estimated_mentions_removed}")
    print(f"  - typed facts removed: {estimated_facts_removed}")
    print("=" * 64)

    return {
        "graph_db": str(graph_path),
        "total_entities": total,
        "junk_entities": len(candidates),
        "reason_counts": dict(reason_counts),
        "estimated_mentions_removed": estimated_mentions_removed,
        "estimated_facts_removed": estimated_facts_removed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit graph entity quality")
    parser.add_argument(
        "--graph-db",
        default=None,
        help="Override graph DB path (defaults to config.GRAPH_DB_PATH).",
    )
    args = parser.parse_args()
    run_audit(args.graph_db)


if __name__ == "__main__":
    main()
