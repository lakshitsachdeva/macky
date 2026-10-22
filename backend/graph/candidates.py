from __future__ import annotations

import hashlib
import re
import time
from typing import Any

try:
    from .store import GraphStore, get_store as get_graph_store
    from ..retrieval import query_parser
except ImportError:
    from graph.store import GraphStore, get_store as get_graph_store
    from retrieval import query_parser


GENERIC_ENTITY_BLOCKLIST = {
    "anyone",
    "company",
    "document",
    "documents",
    "email",
    "emails",
    "everything",
    "file",
    "files",
    "idea",
    "major",
    "meeting",
    "note",
    "plan",
    "project",
    "report",
    "someone",
    "something",
    "startup",
    "team",
    "thing",
    "work",
    "working",
}
CACHE_TTL_SECONDS = 60.0
ENTITY_CACHE_TTL_SECONDS = 300.0


class GraphCandidateGenerator:
    def __init__(self, graph_store: GraphStore | None = None):
        self.graph_store = graph_store or get_graph_store()
        self._query_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._entity_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    def _entity_label(self, entity_id: str) -> str:
        entity = self.graph_store.get_entity(entity_id)
        if entity is None:
            return str(entity_id)
        return str(entity.get("name") or entity_id)

    def _cache_key(self, query: str) -> str:
        normalized = str(query or "").strip().casefold()
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def _cached_entity(self, name: str) -> dict[str, Any] | None:
        key = str(name or "").strip().casefold()
        if not key:
            return None
        cached = self._entity_cache.get(key)
        now = time.time()
        if cached is not None and now - cached[0] <= ENTITY_CACHE_TTL_SECONDS:
            return cached[1]
        entity = self.graph_store.get_entity_by_name(name)
        self._entity_cache[key] = (now, entity)
        return entity

    def extract_seeds(
        self,
        query: str,
        *,
        parsed: query_parser.ParsedQuery | None = None,
    ) -> dict[str, float]:
        parsed_query = parsed or query_parser.parse_query(query)
        candidates: list[str] = []
        candidates.extend(parsed_query.person_hints)
        candidates.extend(re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}(?:\s+[A-Z][A-Za-z0-9_-]{2,})?\b", parsed_query.original))
        candidates.extend(
            token
            for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9 _-]{2,}", parsed_query.semantic_text or "")
            if len(token.strip()) >= 3
        )

        seeds: dict[str, float] = {}
        for raw_candidate in candidates:
            candidate = " ".join(str(raw_candidate or "").split()).strip()
            normalized = candidate.casefold()
            if not candidate or len(candidate) < 3:
                continue
            if normalized in GENERIC_ENTITY_BLOCKLIST:
                continue
            entity = self._cached_entity(candidate)
            if entity is None:
                continue
            fact_count = self.graph_store.get_entity_fact_count(str(entity["id"]))
            weight = 1.0 if fact_count > 0 else 0.3
            seeds[str(entity["name"])] = max(seeds.get(str(entity["name"]), 0.0), weight)
        return seeds

    def generate(
        self,
        query: str,
        *,
        parsed: query_parser.ParsedQuery | None = None,
        n: int = 20,
    ) -> list[dict[str, Any]]:
        cache_key = self._cache_key(query)
        now = time.time()
        cached = self._query_cache.get(cache_key)
        if cached is not None and now - cached[0] <= CACHE_TTL_SECONDS:
            return [dict(entry) for entry in cached[1]]

        started = time.time()
        seeds = self.extract_seeds(query, parsed=parsed)
        if not seeds:
            return []

        candidates: dict[str, dict[str, Any]] = {}
        for seed_name, seed_weight in seeds.items():
            entity = self._cached_entity(seed_name)
            if entity is None:
                continue
            entity_id = str(entity["id"])

            hop1 = self.graph_store.get_neighbors(entity_id, limit=30)
            for neighbor_id, predicate, edge_weight in hop1:
                for episode in self.graph_store.get_episodes_mentioning(neighbor_id, limit=10):
                    score = float(seed_weight) * float(edge_weight) * 1.0
                    entry = candidates.setdefault(
                        str(episode["id"]),
                        {
                            "episode_id": str(episode["id"]),
                            "graph_score": 0.0,
                            "source_pool": "graph",
                            "graph_path": [],
                        },
                    )
                    if score > float(entry["graph_score"]):
                        entry["graph_score"] = round(score, 4)
                    entry["graph_path"] = list(
                        dict.fromkeys(
                            [
                                *entry["graph_path"],
                                str(seed_name),
                                str(predicate),
                                self._entity_label(str(neighbor_id)),
                            ]
                        )
                    )[:8]

                if len(candidates) > 200:
                    break

                hop2 = self.graph_store.get_neighbors(neighbor_id, limit=12)
                for hop2_id, hop2_predicate, hop2_weight in hop2:
                    if hop2_id == entity_id:
                        continue
                    for episode in self.graph_store.get_episodes_mentioning(hop2_id, limit=5):
                        score = float(seed_weight) * float(edge_weight) * float(hop2_weight) * 0.5
                        entry = candidates.setdefault(
                            str(episode["id"]),
                            {
                                "episode_id": str(episode["id"]),
                                "graph_score": 0.0,
                                "source_pool": "graph",
                                "graph_path": [],
                            },
                        )
                        if score > float(entry["graph_score"]):
                            entry["graph_score"] = round(score, 4)
                        entry["graph_path"] = list(
                            dict.fromkeys(
                                [
                                    *entry["graph_path"],
                                    str(seed_name),
                                    str(predicate),
                                    self._entity_label(str(neighbor_id)),
                                    str(hop2_predicate),
                                    self._entity_label(str(hop2_id)),
                                ]
                            )
                        )[:10]
                    if len(candidates) > 200:
                        break
                if len(candidates) > 200:
                    break

        elapsed = time.time() - started
        if elapsed > 0.1:
            import logging

            logging.getLogger(__name__).warning(
                "graph_traversal_timeout: %.0fms, seeds=%s",
                elapsed * 1000.0,
                list(seeds),
            )
        if elapsed > 0.15:
            return []

        ordered = sorted(
            candidates.values(),
            key=lambda entry: float(entry["graph_score"]),
            reverse=True,
        )[: max(1, int(n))]
        self._query_cache[cache_key] = (time.time(), [dict(entry) for entry in ordered])
        return [dict(entry) for entry in ordered]


_generator: GraphCandidateGenerator | None = None


def get_generator() -> GraphCandidateGenerator:
    global _generator
    if _generator is None:
        _generator = GraphCandidateGenerator()
    return _generator
