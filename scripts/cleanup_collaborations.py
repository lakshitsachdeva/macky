#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import config
from backend.graph.store import GraphStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove noisy COLLABORATES_WITH facts from graph.db")
    parser.add_argument("--graph-db", default=None, help="Override graph.db path")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="Minimum confidence to keep")
    parser.add_argument("--no-person-pair-check", action="store_true", help="Do not require Person-Person pairs")
    parser.add_argument("--no-person-like-check", action="store_true", help="Do not require credible person-like names")
    parser.add_argument("--execute", action="store_true", help="Apply deletes. Dry-run is the default.")
    args = parser.parse_args()

    store = GraphStore(args.graph_db or config.GRAPH_DB_PATH)
    summary = store.delete_low_confidence_collaborations(
        min_confidence=float(args.min_confidence),
        require_person_pair=not bool(args.no_person_pair_check),
        require_person_like=not bool(args.no_person_like_check),
        execute=bool(args.execute),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
