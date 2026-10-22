from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .. import config
    from .store import (
        _normalize_datetime,
        _query_variants,
        _utc_now_iso,
        is_meaningful_community_entity,
        normalize_entity_key,
    )
except ImportError:
    import config
    from graph.store import (
        _normalize_datetime,
        _query_variants,
        _utc_now_iso,
        is_meaningful_community_entity,
        normalize_entity_key,
    )


logger = logging.getLogger(__name__)


class FalkorGraphUnavailable(RuntimeError):
    """Raised when FalkorDBLite cannot be loaded in the current Python runtime."""


def _load_falkor_db_class() -> Any:
    try:
        from redislite.falkordb_client import FalkorDB

        return FalkorDB
    except Exception as redislite_error:
        try:
            from falkordblite import FalkorDB  # type: ignore

            return FalkorDB
        except Exception as falkor_error:
            raise FalkorGraphUnavailable(
                "FalkorDBLite is unavailable. Install falkordblite under Python 3.12+ "
                "or keep GRAPH_STORE_BACKEND=sqlite."
            ) from falkor_error or redislite_error


def is_falkor_available() -> bool:
    try:
        _load_falkor_db_class()
        return True
    except FalkorGraphUnavailable:
        return False


class FalkorGraphStore:
    """
    Optional FalkorDBLite graph store for entity nodes and temporal facts.

    Recurdo still defaults to SQLite because the packaged backend currently runs
    on Python 3.11 and falkordblite requires Python 3.12+. This store is kept
    behind GRAPH_STORE_BACKEND=falkor until we migrate the packaged runtime.
    """

    backend_name = "falkor"
    SINGLETON_PREDICATES = {
        "WORKS_ON",
        "HAS_STATUS",
        "IS_EMPLOYED_BY",
        "LOCATED_IN",
        "REPORTS_TO",
        "HAS_PRIORITY",
    }

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        graph_name: str | None = None,
    ):
        FalkorDB = _load_falkor_db_class()
        self.db_path = Path(db_path or config.GRAPH_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph_name = graph_name or getattr(config, "FALKOR_GRAPH_NAME", "recurdo_memory")
        self.db = FalkorDB(str(self.db_path))
        self.graph = self.db.select_graph(self.graph_name)
        self._community_cache_key: tuple[Any, ...] | None = None
        self._community_cache_value: list[dict[str, Any]] | None = None
        self.init_db()

    def get_connection(self) -> None:
        raise NotImplementedError("FalkorGraphStore uses Cypher queries, not sqlite connections")

    def _query(self, query: str, params: dict[str, Any] | None = None, *, readonly: bool = False) -> Any:
        runner = self.graph.ro_query if readonly and hasattr(self.graph, "ro_query") else self.graph.query
        payload = params or {}
        try:
            return runner(query, params=payload)
        except TypeError:
            return runner(query, payload)

    def _result_set(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        readonly: bool = True,
    ) -> list[list[Any]]:
        result = self._query(query, params, readonly=readonly)
        return list(getattr(result, "result_set", []) or [])

    def init_db(self) -> None:
        for query in (
            "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.id)",
            "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.canonical_key)",
            "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name)",
            "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.entity_type)",
            "CREATE INDEX IF NOT EXISTS FOR (ep:Episode) ON (ep.id)",
        ):
            try:
                self._query(query, readonly=False)
            except Exception:
                logger.debug("FalkorDBLite index creation skipped: %s", query, exc_info=True)

    def clear_all(self) -> None:
        self._query("MATCH (n) DETACH DELETE n", readonly=False)
        self._community_cache_key = None
        self._community_cache_value = None

    def clear_bootstrap_state(self) -> None:
        self.clear_all()

    def _entity_props_to_dict(self, props: dict[str, Any] | None) -> dict[str, Any] | None:
        if not props:
            return None
        raw_attributes = props.get("attributes_json") or "{}"
        if isinstance(raw_attributes, dict):
            attributes = raw_attributes
        else:
            try:
                attributes = json.loads(str(raw_attributes or "{}"))
            except json.JSONDecodeError:
                attributes = {}
        aliases = props.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        return {
            "id": str(props.get("id") or ""),
            "name": str(props.get("name") or ""),
            "entity_type": str(props.get("entity_type") or "Concept"),
            "aliases": [str(alias) for alias in aliases if str(alias).strip()],
            "attributes": attributes,
            "mention_count": int(props.get("mention_count") or 0),
            "created_at": str(props.get("created_at") or ""),
            "updated_at": str(props.get("updated_at") or ""),
        }

    def _get_entity_props_by_id(self, entity_id: str) -> dict[str, Any] | None:
        rows = self._result_set(
            """
            MATCH (e:Entity {id: $id})
            RETURN properties(e)
            LIMIT 1
            """,
            {"id": entity_id},
        )
        return dict(rows[0][0]) if rows else None

    def _find_existing_entities(
        self,
        *,
        name: str,
        aliases: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        normalized_candidates = [
            normalized
            for normalized in {
                normalize_entity_key(name),
                *(normalize_entity_key(alias) for alias in (aliases or [])),
            }
            if normalized
        ]
        if not normalized_candidates:
            return []
        rows = self._result_set(
            """
            MATCH (e:Entity)
            WHERE e.canonical_key IN $keys
               OR any(alias IN coalesce(e.normalized_aliases, []) WHERE alias IN $keys)
            RETURN properties(e)
            ORDER BY coalesce(e.mention_count, 0) DESC, coalesce(e.updated_at, "") DESC
            LIMIT $limit
            """,
            {"keys": normalized_candidates, "limit": max(1, int(limit))},
        )
        return [dict(row[0]) for row in rows if row and row[0]]

    def upsert_entity(
        self,
        *,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        cleaned_name = " ".join(str(name).split()).strip()
        if not cleaned_name:
            raise ValueError("Entity name is required")

        merged_aliases = [
            alias
            for alias in dict.fromkeys(
                [cleaned_name, *(str(alias).strip() for alias in (aliases or []) if str(alias).strip())]
            )
            if alias
        ]
        normalized_aliases = [normalize_entity_key(alias) for alias in merged_aliases if normalize_entity_key(alias)]
        now_iso = _utc_now_iso()
        existing_rows = self._find_existing_entities(name=cleaned_name, aliases=merged_aliases)
        if not existing_rows:
            entity_id = str(uuid.uuid4())
            self._query(
                """
                CREATE (:Entity {
                    id: $id,
                    canonical_key: $canonical_key,
                    name: $name,
                    entity_type: $entity_type,
                    aliases: $aliases,
                    normalized_aliases: $normalized_aliases,
                    attributes_json: $attributes_json,
                    mention_count: 0,
                    created_at: $now,
                    updated_at: $now
                })
                """,
                {
                    "id": entity_id,
                    "canonical_key": normalize_entity_key(cleaned_name),
                    "name": cleaned_name,
                    "entity_type": entity_type,
                    "aliases": merged_aliases,
                    "normalized_aliases": normalized_aliases,
                    "attributes_json": json.dumps(attributes or {}, ensure_ascii=True, sort_keys=True),
                    "now": now_iso,
                },
                readonly=False,
            )
            self._community_cache_key = None
            self._community_cache_value = None
            return entity_id

        existing = existing_rows[0]
        entity_id = str(existing["id"])
        existing_aliases = existing.get("aliases") or []
        if isinstance(existing_aliases, str):
            existing_aliases = [existing_aliases]
        combined_aliases = [
            alias
            for alias in dict.fromkeys([*(existing_aliases or []), *merged_aliases])
            if str(alias).strip()
        ]
        combined_normalized_aliases = [
            normalized
            for normalized in dict.fromkeys(normalize_entity_key(alias) for alias in combined_aliases)
            if normalized
        ]
        try:
            existing_attributes = json.loads(str(existing.get("attributes_json") or "{}"))
        except json.JSONDecodeError:
            existing_attributes = {}
        combined_attributes = dict(existing_attributes)
        combined_attributes.update(attributes or {})

        preferred_name = cleaned_name
        if existing.get("name") and len(str(existing["name"])) >= len(cleaned_name):
            preferred_name = str(existing["name"])
        preferred_type = entity_type
        if existing.get("entity_type") and str(existing["entity_type"]) != "Concept":
            preferred_type = str(existing["entity_type"])

        self._query(
            """
            MATCH (e:Entity {id: $id})
            SET e.canonical_key = $canonical_key,
                e.name = $name,
                e.entity_type = $entity_type,
                e.aliases = $aliases,
                e.normalized_aliases = $normalized_aliases,
                e.attributes_json = $attributes_json,
                e.updated_at = $now
            """,
            {
                "id": entity_id,
                "canonical_key": normalize_entity_key(preferred_name),
                "name": preferred_name,
                "entity_type": preferred_type,
                "aliases": combined_aliases,
                "normalized_aliases": combined_normalized_aliases,
                "attributes_json": json.dumps(combined_attributes, ensure_ascii=True, sort_keys=True),
                "now": now_iso,
            },
            readonly=False,
        )
        self._community_cache_key = None
        self._community_cache_value = None
        return entity_id

    def _refresh_mention_count(self, entity_id: str) -> None:
        self._query(
            """
            MATCH (e:Entity {id: $id})
            OPTIONAL MATCH (:Episode)-[m:MENTIONS]->(e)
            WITH e, count(m) AS mention_count
            SET e.mention_count = mention_count,
                e.updated_at = $now
            """,
            {"id": entity_id, "now": _utc_now_iso()},
            readonly=False,
        )

    def link_episode_to_entity(
        self,
        episode_id: str,
        entity_id: str,
        *,
        mentioned_at: datetime | str | None = None,
        relation_type: str = "MENTIONS",
        confidence: float = 1.0,
    ) -> None:
        mentioned_iso = _normalize_datetime(mentioned_at).isoformat()
        self._query(
            """
            MERGE (ep:Episode {id: $episode_id})
            WITH ep
            MATCH (e:Entity {id: $entity_id})
            MERGE (ep)-[m:MENTIONS {relation_type: $relation_type}]->(e)
            ON CREATE SET m.mentioned_at = $mentioned_at,
                          m.confidence = $confidence
            ON MATCH SET m.mentioned_at =
                CASE
                    WHEN coalesce(m.mentioned_at, $mentioned_at) <= $mentioned_at
                    THEN coalesce(m.mentioned_at, $mentioned_at)
                    ELSE $mentioned_at
                END,
                m.confidence =
                CASE
                    WHEN coalesce(m.confidence, 0.0) >= $confidence
                    THEN coalesce(m.confidence, 0.0)
                    ELSE $confidence
                END
            """,
            {
                "episode_id": episode_id,
                "entity_id": entity_id,
                "relation_type": relation_type,
                "mentioned_at": mentioned_iso,
                "confidence": float(confidence),
            },
            readonly=False,
        )
        self._refresh_mention_count(entity_id)
        self._community_cache_key = None
        self._community_cache_value = None

    def add_temporal_fact(
        self,
        *,
        subject_id: str,
        predicate: str,
        object_id: str,
        valid_from: datetime | str,
        episode_id: str,
        confidence: float = 1.0,
    ) -> str:
        valid_from_iso = _normalize_datetime(valid_from).isoformat()
        if predicate in self.SINGLETON_PREDICATES:
            self._query(
                """
                MATCH (:Entity {id: $subject_id})-[f:FACT {predicate: $predicate}]->(:Entity)
                WHERE f.valid_until IS NULL
                  AND f.valid_from <= $valid_from
                SET f.valid_until = $valid_from
                """,
                {
                    "subject_id": subject_id,
                    "predicate": predicate,
                    "valid_from": valid_from_iso,
                },
                readonly=False,
            )

        fact_id = str(uuid.uuid4())
        self._query(
            """
            MATCH (subject:Entity {id: $subject_id})
            MATCH (object:Entity {id: $object_id})
            CREATE (subject)-[:FACT {
                id: $fact_id,
                predicate: $predicate,
                valid_from: $valid_from,
                valid_until: NULL,
                episode_id: $episode_id,
                confidence: $confidence,
                created_at: $created_at
            }]->(object)
            """,
            {
                "subject_id": subject_id,
                "object_id": object_id,
                "fact_id": fact_id,
                "predicate": predicate,
                "valid_from": valid_from_iso,
                "episode_id": episode_id,
                "confidence": float(confidence),
                "created_at": _utc_now_iso(),
            },
            readonly=False,
        )
        self._community_cache_key = None
        self._community_cache_value = None
        return fact_id

    def clear_episode_state(self, episode_id: str) -> None:
        if not episode_id:
            return
        mention_rows = self._result_set(
            """
            MATCH (:Episode {id: $episode_id})-[:MENTIONS]->(e:Entity)
            RETURN DISTINCT e.id
            """,
            {"episode_id": episode_id},
        )
        fact_rows = self._result_set(
            """
            MATCH (s:Entity)-[f:FACT {episode_id: $episode_id}]->(o:Entity)
            RETURN DISTINCT s.id, o.id
            """,
            {"episode_id": episode_id},
        )
        affected_ids = {str(row[0]) for row in mention_rows if row and row[0]}
        for row in fact_rows:
            if row and row[0]:
                affected_ids.add(str(row[0]))
            if len(row) > 1 and row[1]:
                affected_ids.add(str(row[1]))

        self._query(
            """
            MATCH (:Episode {id: $episode_id})-[m:MENTIONS]->(:Entity)
            DELETE m
            """,
            {"episode_id": episode_id},
            readonly=False,
        )
        self._query(
            """
            MATCH (:Entity)-[f:FACT {episode_id: $episode_id}]->(:Entity)
            DELETE f
            """,
            {"episode_id": episode_id},
            readonly=False,
        )
        self._query(
            """
            MATCH (ep:Episode {id: $episode_id})
            WHERE NOT (ep)--()
            DELETE ep
            """,
            {"episode_id": episode_id},
            readonly=False,
        )
        for entity_id in affected_ids:
            self._refresh_mention_count(entity_id)
        self._query(
            """
            MATCH (e:Entity)
            WHERE coalesce(e.mention_count, 0) <= 0
              AND NOT (e)-[:FACT]->(:Entity)
              AND NOT (:Entity)-[:FACT]->(e)
            DETACH DELETE e
            """,
            readonly=False,
        )
        self._community_cache_key = None
        self._community_cache_value = None

    def get_counts(self) -> dict[str, int]:
        rows = self._result_set(
            """
            MATCH (e:Entity)
            RETURN count(e), coalesce(sum(size(coalesce(e.normalized_aliases, []))), 0)
            """,
        )
        entities = int(rows[0][0] or 0) if rows else 0
        aliases = int(rows[0][1] or 0) if rows else 0
        mention_rows = self._result_set("MATCH ()-[m:MENTIONS]->() RETURN count(m)")
        fact_rows = self._result_set("MATCH ()-[f:FACT]->() RETURN count(f)")
        return {
            "entities": entities,
            "aliases": aliases,
            "episode_mentions": int(mention_rows[0][0] or 0) if mention_rows else 0,
            "temporal_facts": int(fact_rows[0][0] or 0) if fact_rows else 0,
        }

    def _score_entity_match(self, query: str, entity: dict[str, Any]) -> float:
        normalized_query = normalize_entity_key(query)
        if not normalized_query:
            return 0.0
        aliases = [normalize_entity_key(alias) for alias in entity.get("aliases", [])]
        fields = [normalize_entity_key(entity["name"]), *aliases]
        query_tokens = set(normalized_query.split())
        if not query_tokens:
            return 0.0
        best = 0.0
        for field in fields:
            if not field:
                continue
            field_tokens = set(field.split())
            token_overlap = len(query_tokens & field_tokens) / max(1, len(query_tokens))
            score = token_overlap * 0.65
            if field == normalized_query:
                score += 1.0
            elif normalized_query in field:
                score += 0.75
            elif field in normalized_query:
                score += 0.55
            best = max(best, score)
        mention_bonus = min(0.25, math.log1p(int(entity.get("mention_count") or 0)) / 10.0)
        return best + mention_bonus

    def get_entity_by_name(self, entity_name: str) -> dict[str, Any] | None:
        normalized = normalize_entity_key(entity_name)
        if not normalized:
            return None
        rows = self._result_set(
            """
            MATCH (e:Entity)
            WHERE e.id = $raw
               OR e.canonical_key = $normalized
               OR toLower(e.name) = $lowered
               OR any(alias IN coalesce(e.normalized_aliases, []) WHERE alias = $normalized)
            RETURN properties(e)
            ORDER BY
                CASE
                    WHEN e.canonical_key = $normalized THEN 0
                    WHEN toLower(e.name) = $lowered THEN 1
                    ELSE 2
                END,
                coalesce(e.mention_count, 0) DESC,
                coalesce(e.updated_at, "") DESC
            LIMIT 1
            """,
            {"raw": entity_name, "normalized": normalized, "lowered": str(entity_name).lower()},
        )
        if rows:
            return self._entity_props_to_dict(dict(rows[0][0]))
        matches = self.search_entities(entity_name, limit=1)
        return matches[0] if matches else None

    def search_entities(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        variants = _query_variants(query)
        if not variants:
            return []
        rows = self._result_set(
            """
            MATCH (e:Entity)
            WHERE any(variant IN $variants WHERE
                e.canonical_key = variant
                OR e.canonical_key CONTAINS variant
                OR toLower(e.name) CONTAINS variant
                OR any(alias IN coalesce(e.normalized_aliases, []) WHERE alias = variant OR alias CONTAINS variant)
            )
            RETURN DISTINCT properties(e)
            LIMIT $limit
            """,
            {"variants": variants, "limit": max(int(limit) * 5, 25)},
        )
        scored_matches: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            entity = self._entity_props_to_dict(dict(row[0]))
            if entity is None:
                continue
            score = self._score_entity_match(query, entity)
            if score <= 0:
                continue
            entity["match_score"] = round(score, 3)
            scored_matches.append((score, entity))
        scored_matches.sort(
            key=lambda entry: (
                entry[0],
                int(entry[1]["mention_count"]),
                entry[1]["name"],
            ),
            reverse=True,
        )
        return [entity for _, entity in scored_matches[:limit]]

    def get_episode_entities(
        self,
        episode_ids: list[str],
        *,
        limit_per_episode: int = 12,
    ) -> dict[str, list[dict[str, Any]]]:
        unique_episode_ids = [episode_id for episode_id in dict.fromkeys(episode_ids) if episode_id]
        if not unique_episode_ids:
            return {}
        rows = self._result_set(
            """
            MATCH (ep:Episode)-[m:MENTIONS]->(e:Entity)
            WHERE ep.id IN $episode_ids
            RETURN ep.id, properties(m), properties(e)
            ORDER BY ep.id ASC, coalesce(m.confidence, 0.0) DESC, coalesce(e.mention_count, 0) DESC, e.name ASC
            """,
            {"episode_ids": unique_episode_ids},
        )
        grouped: dict[str, list[dict[str, Any]]] = {episode_id: [] for episode_id in unique_episode_ids}
        for row in rows:
            episode_id = str(row[0])
            mention_props = dict(row[1])
            entity = self._entity_props_to_dict(dict(row[2]))
            if entity is None:
                continue
            entity["mention_confidence"] = float(mention_props.get("confidence") or 0.0)
            entity["relation_type"] = str(mention_props.get("relation_type") or "MENTIONS")
            entity["mentioned_at"] = str(mention_props.get("mentioned_at") or "")
            bucket = grouped.setdefault(episode_id, [])
            if len(bucket) < limit_per_episode:
                bucket.append(entity)
        return grouped

    def get_facts_at_time(
        self,
        entity_id: str,
        point_in_time: datetime | str,
    ) -> list[dict[str, Any]]:
        point_iso = _normalize_datetime(point_in_time).isoformat()
        rows = self._result_set(
            """
            MATCH (:Entity {id: $entity_id})-[f:FACT]->(object:Entity)
            WHERE f.valid_from <= $point
              AND (f.valid_until IS NULL OR f.valid_until > $point)
            RETURN f.predicate, object.name, object.entity_type, f.valid_from, f.valid_until, f.confidence, f.episode_id
            ORDER BY f.valid_from ASC
            """,
            {"entity_id": entity_id, "point": point_iso},
        )
        return [
            {
                "predicate": str(row[0]),
                "object": str(row[1]),
                "object_type": str(row[2]),
                "valid_from": str(row[3]),
                "valid_until": row[4],
                "confidence": float(row[5] or 0.0),
                "episode_id": str(row[6]),
            }
            for row in rows
        ]

    def get_entity_timeline(self, entity_name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        entity = self.get_entity_by_name(entity_name)
        if entity is None:
            return []
        rows = self._result_set(
            """
            MATCH (subject:Entity)-[f:FACT]->(object:Entity)
            WHERE subject.id = $entity_id OR object.id = $entity_id
            RETURN subject.name, f.predicate, object.name, f.valid_from, f.valid_until, f.episode_id, f.confidence
            ORDER BY f.valid_from ASC, f.created_at ASC
            LIMIT $limit
            """,
            {"entity_id": str(entity["id"]), "limit": max(1, int(limit))},
        )
        return [
            {
                "subject": str(row[0]),
                "predicate": str(row[1]),
                "object": str(row[2]),
                "valid_from": str(row[3]),
                "valid_until": row[4],
                "episode_id": str(row[5]),
                "confidence": float(row[6] or 0.0),
                "is_current": row[4] is None,
            }
            for row in rows
        ]

    def get_episode_mentions(
        self,
        entity_name: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        entity = self.get_entity_by_name(entity_name)
        if entity is None:
            return []
        rows = self._result_set(
            """
            MATCH (ep:Episode)-[m:MENTIONS]->(:Entity {id: $entity_id})
            RETURN ep.id, m.relation_type, m.mentioned_at, m.confidence
            ORDER BY m.mentioned_at ASC, ep.id ASC
            LIMIT $limit
            """,
            {"entity_id": str(entity["id"]), "limit": max(1, int(limit))},
        )
        return [
            {
                "episode_id": str(row[0]),
                "relation_type": str(row[1]),
                "mentioned_at": str(row[2]),
                "confidence": float(row[3] or 0.0),
            }
            for row in rows
        ]

    def get_entities_active_between(
        self,
        start: datetime | str,
        end: datetime | str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        start_iso = _normalize_datetime(start).isoformat()
        end_iso = _normalize_datetime(end).isoformat()
        rows = self._result_set(
            """
            MATCH (:Episode)-[m:MENTIONS]->(e:Entity)
            WHERE m.mentioned_at >= $start AND m.mentioned_at <= $end
            WITH e, count(m) AS period_mentions, max(m.mentioned_at) AS last_mentioned_at
            RETURN properties(e), period_mentions, last_mentioned_at
            ORDER BY period_mentions DESC, last_mentioned_at DESC, e.name ASC
            LIMIT $limit
            """,
            {"start": start_iso, "end": end_iso, "limit": max(1, int(limit))},
        )
        entities: list[dict[str, Any]] = []
        for row in rows:
            entity = self._entity_props_to_dict(dict(row[0]))
            if entity is None:
                continue
            entity["period_mentions"] = int(row[1] or 0)
            entity["last_mentioned_at"] = str(row[2] or "")
            entities.append(entity)
        return entities

    def score_items_for_query_entities(
        self,
        query_entities: list[str],
        item_ids: list[str],
        *,
        limit_per_query: int = 3,
    ) -> dict[str, Any]:
        unique_item_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
        if not unique_item_ids or not query_entities:
            return {
                "scores": {},
                "seed_entities": [],
                "direct_matches": {},
                "neighbor_matches": {},
            }

        seed_weights: dict[str, float] = {}
        raw_seed_entities: list[dict[str, Any]] = []
        for raw_query in query_entities:
            cleaned = str(raw_query).removeprefix("entity:").replace("-", " ").replace("_", " ").strip()
            if not cleaned:
                continue
            matches = self.search_entities(cleaned, limit=limit_per_query)
            for rank, entity in enumerate(matches):
                entity_id = str(entity["id"])
                weight = max(0.2, 1.0 - (rank * 0.2))
                match_score = float(entity.get("match_score") or 0.0)
                seed_weights[entity_id] = max(seed_weights.get(entity_id, 0.0), min(1.0, weight * max(0.45, match_score)))
            if matches:
                raw_seed_entities.append(
                    {
                        "query": cleaned,
                        "matches": [
                            {
                                "id": str(entity["id"]),
                                "name": str(entity["name"]),
                                "entity_type": str(entity["entity_type"]),
                                "match_score": float(entity.get("match_score") or 0.0),
                            }
                            for entity in matches
                        ],
                    }
                )

        if not seed_weights:
            return {
                "scores": {},
                "seed_entities": [],
                "direct_matches": {},
                "neighbor_matches": {},
            }

        episode_entities = self.get_episode_entities(unique_item_ids)
        seed_ids = list(seed_weights)
        temporal_rows = self._result_set(
            """
            MATCH (seed:Entity)-[f:FACT]-(neighbor:Entity)
            WHERE seed.id IN $seed_ids AND NOT neighbor.id IN $seed_ids
            RETURN neighbor.id, sum(coalesce(f.confidence, 1.0))
            """,
            {"seed_ids": seed_ids},
        )
        shared_rows = self._result_set(
            """
            MATCH (seed:Entity)<-[:MENTIONS]-(:Episode)-[:MENTIONS]->(neighbor:Entity)
            WHERE seed.id IN $seed_ids AND NOT neighbor.id IN $seed_ids
            RETURN neighbor.id, count(*)
            """,
            {"seed_ids": seed_ids},
        )

        neighbor_weights: dict[str, float] = {}
        for row in temporal_rows:
            neighbor_id = str(row[0])
            if neighbor_id in seed_weights:
                continue
            neighbor_weights[neighbor_id] = max(
                neighbor_weights.get(neighbor_id, 0.0),
                min(0.9, 0.25 + (float(row[1] or 0.0) * 0.35)),
            )
        for row in shared_rows:
            neighbor_id = str(row[0])
            if neighbor_id in seed_weights:
                continue
            neighbor_weights[neighbor_id] = max(
                neighbor_weights.get(neighbor_id, 0.0),
                min(0.8, 0.2 + (float(row[1] or 0.0) * 0.18)),
            )

        scores: dict[str, float] = {}
        direct_matches: dict[str, list[str]] = {}
        neighbor_matches: dict[str, list[str]] = {}
        for item_id, entities in episode_entities.items():
            direct_names: list[str] = []
            neighbor_names: list[str] = []
            direct_score = 0.0
            neighbor_score = 0.0
            for entity in entities:
                entity_id = str(entity["id"])
                entity_name = str(entity["name"])
                if entity_id in seed_weights:
                    direct_score += seed_weights[entity_id]
                    direct_names.append(entity_name)
                elif entity_id in neighbor_weights:
                    neighbor_score += neighbor_weights[entity_id]
                    neighbor_names.append(entity_name)
            graph_score = min(
                1.0,
                min(1.0, direct_score) * 0.75
                + min(1.0, neighbor_score) * 0.35,
            )
            if graph_score <= 0:
                continue
            scores[item_id] = round(graph_score, 4)
            if direct_names:
                direct_matches[item_id] = list(dict.fromkeys(direct_names))[:5]
            if neighbor_names:
                neighbor_matches[item_id] = list(dict.fromkeys(neighbor_names))[:5]

        return {
            "scores": scores,
            "seed_entities": raw_seed_entities,
            "direct_matches": direct_matches,
            "neighbor_matches": neighbor_matches,
        }

    def detect_communities(
        self,
        *,
        limit: int = 12,
        min_size: int = 3,
        min_shared_mentions: int = 2,
        min_mentions_per_entity: int = 2,
    ) -> list[dict[str, Any]]:
        cache_key = (
            self.db_path.stat().st_mtime_ns if self.db_path.exists() else 0,
            limit,
            min_size,
            min_shared_mentions,
            min_mentions_per_entity,
        )
        if self._community_cache_key == cache_key and self._community_cache_value is not None:
            return [dict(community) for community in self._community_cache_value[:limit]]

        entity_rows = self._result_set(
            """
            MATCH (e:Entity)
            WHERE coalesce(e.mention_count, 0) >= $min_mentions
            RETURN properties(e)
            """,
            {"min_mentions": int(min_mentions_per_entity)},
        )
        entities = {
            str(entity["id"]): entity
            for entity in (
                self._entity_props_to_dict(dict(row[0]))
                for row in entity_rows
                if row and row[0]
            )
            if entity is not None and is_meaningful_community_entity(str(entity["name"]))
        }

        co_rows = self._result_set(
            """
            MATCH (left:Entity)<-[:MENTIONS]-(ep:Episode)-[:MENTIONS]->(right:Entity)
            WHERE left.id < right.id
              AND coalesce(left.mention_count, 0) >= $min_mentions
              AND coalesce(right.mention_count, 0) >= $min_mentions
            WITH left, right, count(DISTINCT ep.id) AS shared_episodes
            WHERE shared_episodes >= $min_shared
            RETURN left.id, right.id, shared_episodes
            """,
            {
                "min_mentions": int(min_mentions_per_entity),
                "min_shared": int(min_shared_mentions),
            },
        )
        fact_rows = self._result_set(
            """
            MATCH (left:Entity)-[f:FACT]->(right:Entity)
            RETURN left.id, right.id, count(f)
            """
        )

        adjacency: dict[str, set[str]] = {}
        edge_weights: dict[tuple[str, str], float] = {}

        def connect(left_id: str, right_id: str, weight: float) -> None:
            if left_id == right_id or left_id not in entities or right_id not in entities:
                return
            adjacency.setdefault(left_id, set()).add(right_id)
            adjacency.setdefault(right_id, set()).add(left_id)
            edge_key = tuple(sorted((left_id, right_id)))
            edge_weights[edge_key] = max(edge_weights.get(edge_key, 0.0), weight)

        for row in co_rows:
            connect(str(row[0]), str(row[1]), 0.4 + (float(row[2] or 0.0) * 0.3))
        for row in fact_rows:
            connect(str(row[0]), str(row[1]), 0.8 + (float(row[2] or 0.0) * 0.35))

        communities: list[dict[str, Any]] = []
        visited: set[str] = set()
        for entity_id in sorted(adjacency):
            if entity_id in visited:
                continue
            component: list[str] = []
            queue: deque[str] = deque([entity_id])
            visited.add(entity_id)
            while queue:
                current = queue.popleft()
                component.append(current)
                for neighbor_id in adjacency.get(current, set()):
                    if neighbor_id in visited:
                        continue
                    visited.add(neighbor_id)
                    queue.append(neighbor_id)

            if len(component) < min_size:
                continue
            member_entities = [entities[current_id] for current_id in component if current_id in entities]
            member_entities.sort(
                key=lambda entity: (
                    int(entity["mention_count"]),
                    entity["name"],
                ),
                reverse=True,
            )
            component_set = set(component)
            episode_rows = self._result_set(
                """
                MATCH (ep:Episode)-[:MENTIONS]->(e:Entity)
                WHERE e.id IN $entity_ids
                RETURN DISTINCT ep.id
                """,
                {"entity_ids": component},
            )
            episode_ids = [str(row[0]) for row in episode_rows if row and row[0]]
            last_rows = self._result_set(
                """
                MATCH (:Episode)-[m:MENTIONS]->(e:Entity)
                WHERE e.id IN $entity_ids
                RETURN max(m.mentioned_at)
                """,
                {"entity_ids": component},
            )
            last_activity = str(last_rows[0][0] or "") if last_rows else ""
            edge_count = sum(1 for edge in edge_weights if edge[0] in component_set and edge[1] in component_set)
            activity_score = min(
                1.0,
                (
                    sum(int(entity["mention_count"]) for entity in member_entities)
                    / max(1, len(member_entities) * 10)
                ),
            )
            type_priority = {
                "Project": 0,
                "Concept": 1,
                "Document": 2,
                "Organisation": 3,
                "Person": 4,
            }
            label_candidates = sorted(
                member_entities,
                key=lambda entity: (
                    type_priority.get(str(entity["entity_type"]), 5),
                    -int(entity["mention_count"]),
                    -len(normalize_entity_key(entity["name"])),
                    str(entity["name"]),
                ),
            )
            representative_names = [
                str(entity["name"])
                for entity in label_candidates
                if is_meaningful_community_entity(str(entity["name"]))
            ][:4]
            label_root = representative_names[0] if representative_names else "Memory"
            top_entities = [
                {
                    "id": str(entity["id"]),
                    "name": str(entity["name"]),
                    "entity_type": str(entity["entity_type"]),
                    "mention_count": int(entity["mention_count"]),
                }
                for entity in member_entities
                if is_meaningful_community_entity(str(entity["name"]))
            ][:6]
            if len(top_entities) < min_size:
                continue
            community_id = "community:" + hashlib.sha1(
                "|".join(sorted(component)).encode("utf-8")
            ).hexdigest()[:12]
            communities.append(
                {
                    "id": community_id,
                    "label": f"{label_root} Cluster",
                    "entity_count": len(member_entities),
                    "edge_count": edge_count,
                    "episode_count": len(episode_ids),
                    "activity_score": round(activity_score, 3),
                    "last_activity": last_activity,
                    "top_entities": top_entities,
                    "sample_episode_ids": episode_ids[:8],
                    "member_entity_ids": sorted(component),
                }
            )

        communities.sort(
            key=lambda community: (
                float(community["activity_score"]),
                int(community["episode_count"]),
                int(community["entity_count"]),
            ),
            reverse=True,
        )
        self._community_cache_key = cache_key
        self._community_cache_value = [dict(community) for community in communities]
        return [dict(community) for community in communities[:limit]]

    def get_community(self, community_id: str) -> dict[str, Any] | None:
        for community in self.detect_communities(limit=100):
            if str(community.get("id")) == str(community_id):
                return community
        return None

    def find_connections(self, entity_id: str, *, depth: int = 2, limit: int = 50) -> list[dict[str, Any]]:
        target = self.get_entity_by_name(entity_id)
        if target is None:
            return []
        start_id = str(target["id"])
        visited = {start_id}
        frontier: deque[tuple[str, int]] = deque([(start_id, 0)])
        discovered: dict[str, int] = {}

        while frontier:
            current_id, current_depth = frontier.popleft()
            if current_depth >= depth:
                continue
            temporal_rows = self._result_set(
                """
                MATCH (:Entity {id: $entity_id})-[:FACT]-(neighbor:Entity)
                RETURN DISTINCT neighbor.id
                """,
                {"entity_id": current_id},
            )
            shared_rows = self._result_set(
                """
                MATCH (:Entity {id: $entity_id})<-[:MENTIONS]-(:Episode)-[:MENTIONS]->(neighbor:Entity)
                WHERE neighbor.id <> $entity_id
                RETURN DISTINCT neighbor.id
                """,
                {"entity_id": current_id},
            )
            for row in [*temporal_rows, *shared_rows]:
                neighbor_id = str(row[0])
                if neighbor_id in visited:
                    continue
                visited.add(neighbor_id)
                discovered[neighbor_id] = current_depth + 1
                frontier.append((neighbor_id, current_depth + 1))
                if len(discovered) >= limit:
                    frontier.clear()
                    break

        if not discovered:
            return []
        rows = self._result_set(
            """
            MATCH (e:Entity)
            WHERE e.id IN $entity_ids
            RETURN properties(e)
            """,
            {"entity_ids": list(discovered)},
        )
        connections: list[dict[str, Any]] = []
        for row in rows:
            entity = self._entity_props_to_dict(dict(row[0]))
            if entity is None:
                continue
            entity["hops"] = discovered[str(entity["id"])]
            connections.append(entity)
        connections.sort(key=lambda entity: (int(entity["hops"]), -int(entity["mention_count"]), entity["name"]))
        return connections[:limit]
