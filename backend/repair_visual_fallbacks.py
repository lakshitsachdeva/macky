#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from . import database
    from .indexer.pipeline import _apply_deferred_visual_ocr_sync
except ImportError:
    import database
    from indexer.pipeline import _apply_deferred_visual_ocr_sync


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair legacy visual items that were indexed with weak filename-only "
            "fallback captions instead of useful OCR text."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of visual items to process.",
    )
    parser.add_argument(
        "--contains",
        default="",
        help="Only process items whose path contains this case-insensitive substring.",
    )
    parser.add_argument(
        "--all-visual",
        action="store_true",
        help="Process all visual items, not just legacy fallback-caption items.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be repaired without changing the database.",
    )
    return parser.parse_args()


def load_candidates(
    conn,
    *,
    limit: int,
    contains: str,
    all_visual: bool,
) -> list[dict]:
    where_clauses = [
        "COALESCE(is_excluded, 0) = 0",
        "handler_type = 'visual'",
        "original_path IS NOT NULL",
        "TRIM(original_path) <> ''",
    ]
    params: list[object] = []

    if not all_visual:
        where_clauses.append(
            "("
            "TRIM(COALESCE(extracted_text, '')) = '' "
            "OR extracted_text LIKE 'Image file %'"
            ")"
        )

    contains_value = str(contains or "").strip().casefold()
    if contains_value:
        where_clauses.append("LOWER(original_path) LIKE ?")
        params.append(f"%{contains_value}%")

    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT id, filename, original_path, extracted_text, first_seen_at, last_opened_at
        FROM items
        WHERE {' AND '.join(where_clauses)}
        ORDER BY COALESCE(last_opened_at, first_seen_at, created_at) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def main() -> int:
    args = parse_args()
    conn = database.get_connection()
    try:
        candidates = load_candidates(
            conn,
            limit=args.limit,
            contains=args.contains,
            all_visual=args.all_visual,
        )
    finally:
        conn.close()

    if not candidates:
        print("No matching visual items found.")
        return 0

    print(f"Found {len(candidates)} matching visual items.")

    repaired = 0
    skipped = 0
    failed = 0

    for row in candidates:
        item_id = str(row["id"])
        path = Path(str(row["original_path"]))
        preview = str(row.get("extracted_text") or "").strip().replace("\n", " ")[:120]

        print(f"\n- {row.get('filename') or path.name}")
        print(f"  path: {path}")
        print(f"  current text: {preview or '[empty]'}")

        if not path.exists() or path.is_dir():
            skipped += 1
            print("  result: skipped (missing on disk)")
            continue

        if args.dry_run:
            print("  result: dry-run only")
            continue

        result = _apply_deferred_visual_ocr_sync(item_id, path)
        status = str(result.get("status") or "")
        if status == "updated":
            repaired += 1
            print(f"  result: updated ({result.get('text_source') or 'unknown'})")
        elif status == "skipped":
            skipped += 1
            print(f"  result: skipped ({result.get('reason') or 'unknown'})")
        else:
            failed += 1
            print(f"  result: error ({result.get('error') or 'unknown'})")

    print("\nSummary")
    print(f"  repaired: {repaired}")
    print(f"  skipped:  {skipped}")
    print(f"  failed:   {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
