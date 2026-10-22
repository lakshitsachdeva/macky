from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

import backend.database as db
from backend.indexer import pipeline


AIMA_NAME = (
    "Stuart J. Russel, Peter Norvig - Artificial Intelligence_ A Modern Approach "
    "(2009, Pearson Education Inc.) - libgen.li.pdf"
)


def find_stale_large_files() -> list[Path]:
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT i.id, i.original_path, i.filename,
               COUNT(CASE WHEN e.embedding_type='dense' THEN 1 END) AS dense_count
        FROM items i
        LEFT JOIN embeddings e ON e.item_id = i.id
        WHERE i.file_type IN ('pdf', 'docx', 'text')
          AND i.is_excluded = 0
        GROUP BY i.id, i.original_path, i.filename
        """
    ).fetchall()
    conn.close()

    files: list[tuple[bool, int, Path]] = []
    for row in rows:
        path = Path(row["original_path"])
        if not path.exists() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 500_000:
            continue
        if int(row["dense_count"] or 0) > 1:
            continue
        files.append((path.name != AIMA_NAME, size, path))

    files.sort(key=lambda item: (item[0], item[1]))
    return [path for _, _, path in files]


async def main() -> None:
    paths = find_stale_large_files()
    print(f"Found {len(paths)} stale large files to re-index", flush=True)
    await pipeline.ensure_model_loaded()

    for idx, path in enumerate(paths, 1):
        try:
            size_mb = path.stat().st_size / 1024 / 1024
        except OSError:
            print(f"[{idx}/{len(paths)}] Missing: {path}", flush=True)
            continue

        print(
            f"[{idx}/{len(paths)}] Re-indexing {path.name} ({size_mb:.1f}MB)",
            flush=True,
        )
        result = await pipeline.index_file(path, force=True)
        print(f"[{idx}/{len(paths)}] Result: {result}", flush=True)

    print("Re-indexing complete", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
