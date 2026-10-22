#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import config
from backend.graph.enrichment import backfill_document_co_occurrences, backfill_email_facts
from backend.graph.store import GraphStore


def _progress(payload: dict[str, object]) -> None:
    stage = str(payload.get("stage") or "backfill")
    processed = int(payload.get("processed") or 0)
    total = int(payload.get("total") or 0)
    print(f"[{stage}] {processed}/{total} {json.dumps(payload, ensure_ascii=True)}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill graph enrichment facts over an existing Recurdo library")
    parser.add_argument("--memory-db", default=str(config.DB_PATH))
    parser.add_argument("--graph-db", default=str(config.GRAPH_DB_PATH))
    parser.add_argument("--email-limit", type=int, default=None)
    parser.add_argument("--doc-limit", type=int, default=None)
    parser.add_argument("--skip-email", action="store_true")
    parser.add_argument("--skip-docs", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    graph_store = GraphStore(Path(args.graph_db).expanduser())
    summary: dict[str, object] = {
        "memory_db": str(Path(args.memory_db).expanduser()),
        "graph_db": str(Path(args.graph_db).expanduser()),
    }

    if not args.skip_email:
        summary["email"] = backfill_email_facts(
            memory_db_path=args.memory_db,
            graph_store=graph_store,
            limit=args.email_limit,
            progress_callback=_progress,
        )
    if not args.skip_docs:
        summary["documents"] = backfill_document_co_occurrences(
            memory_db_path=args.memory_db,
            graph_store=graph_store,
            limit=args.doc_limit,
            progress_callback=_progress,
        )

    print()
    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
