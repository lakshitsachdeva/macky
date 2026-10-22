from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .. import config
    from ..graph.store import GraphStore, get_store as get_graph_store
    from . import query_parser, search as retrieval_search
except ImportError:
    import config
    from graph.store import GraphStore, get_store as get_graph_store
    from retrieval import query_parser, search as retrieval_search


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _best_timestamp(item: dict[str, Any]) -> datetime:
    for field in ("first_seen_at", "created_at", "modified_at", "last_opened_at"):
        parsed = _parse_iso(item.get(field))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


class TemporalQueryEngine:
    def __init__(
        self,
        graph_store: GraphStore | None = None,
        *,
        memory_db_path: str | Path | None = None,
    ):
        self.graph_store = graph_store or get_graph_store()
        self.memory_db_path = Path(memory_db_path or config.DB_PATH).expanduser()

    def _open_memory_db_readonly(self) -> sqlite3.Connection:
        resolved = self.memory_db_path.expanduser().resolve()
        uri = f"{resolved.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _resolve_window(self, query: str) -> query_parser.TimeWindow:
        parsed = query_parser.parse_query(query)
        if parsed.time_window is not None:
            return parsed.time_window
        now = datetime.now(timezone.utc)
        return query_parser.TimeWindow(
            description="last 30 days",
            start_date=now - timedelta(days=30),
            end_date=now,
            center_date=now - timedelta(days=15),
            size_days=30,
        )

    def _resolve_entity_anchor(
        self,
        parsed: query_parser.ParsedQuery,
    ) -> dict[str, Any] | None:
        candidates: list[str] = []
        candidates.extend(parsed.person_hints)
        candidates.extend(re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}(?:\s+[A-Z][A-Za-z0-9_-]{2,})?\b", parsed.original))
        semantic_tokens = [
            token
            for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9 _-]{2,}", parsed.semantic_text or "")
            if len(token.strip()) >= 3
        ]
        candidates.extend(semantic_tokens)

        seen: set[str] = set()
        for candidate in candidates:
            cleaned = " ".join(str(candidate or "").split()).strip()
            normalized = cleaned.casefold()
            if not cleaned or normalized in seen:
                continue
            seen.add(normalized)
            entity = self.graph_store.get_entity_by_name(cleaned)
            if entity is not None:
                return entity
        return None

    def _prefers_document_mode(self, parsed: query_parser.ParsedQuery) -> bool:
        lowered = parsed.original.casefold()
        if parsed.file_type or parsed.handler_type:
            return True
        return any(
            phrase in lowered
            for phrase in (
                "file",
                "files",
                "document",
                "documents",
                "doc",
                "docs",
                "pdf",
                "ppt",
                "slides",
                "sheet",
                "spreadsheet",
                "report",
                "notes",
            )
        )

    def _load_items_in_window(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        start_iso = start.astimezone(timezone.utc).isoformat()
        end_iso = end.astimezone(timezone.utc).isoformat()
        conn = self._open_memory_db_readonly()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM items
                WHERE COALESCE(is_excluded, 0) = 0
                  AND (
                    (first_seen_at IS NOT NULL AND first_seen_at >= ? AND first_seen_at <= ?)
                    OR (created_at IS NOT NULL AND created_at >= ? AND created_at <= ?)
                    OR (modified_at IS NOT NULL AND modified_at >= ? AND modified_at <= ?)
                    OR (last_opened_at IS NOT NULL AND last_opened_at >= ? AND last_opened_at <= ?)
                  )
                ORDER BY COALESCE(last_opened_at, modified_at, created_at, first_seen_at) DESC
                LIMIT ?
                """,
                (
                    start_iso,
                    end_iso,
                    start_iso,
                    end_iso,
                    start_iso,
                    end_iso,
                    start_iso,
                    end_iso,
                    limit,
                ),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def _load_items_by_ids(self, item_ids: list[str]) -> list[dict[str, Any]]:
        unique_item_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
        if not unique_item_ids:
            return []

        placeholders = ", ".join("?" for _ in unique_item_ids)
        conn = self._open_memory_db_readonly()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM items
                WHERE id IN ({placeholders})
                  AND COALESCE(is_excluded, 0) = 0
                """,
                unique_item_ids,
            ).fetchall()
        finally:
            conn.close()

        item_map = {str(row["id"]): dict(row) for row in rows}
        return [item_map[item_id] for item_id in unique_item_ids if item_id in item_map]

    def activity_mode(
        self,
        start: datetime,
        end: datetime,
        *,
        topic: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        active_entities = self.graph_store.get_entities_active_between(start, end, limit=max(limit * 3, 20))
        filtered_entities = active_entities
        if topic:
            topic_matches = self.graph_store.search_entities(topic, limit=5)
            topic_names = {
                str(entity["name"]).casefold()
                for entity in topic_matches
            } | {str(topic).casefold()}
            narrowed = [
                entity
                for entity in active_entities
                if str(entity.get("name") or "").casefold() in topic_names
                or any(
                    str(topic).casefold() in str(alias).casefold()
                    for alias in entity.get("aliases") or []
                )
            ]
            if narrowed:
                filtered_entities = narrowed

        episodes = self._load_items_in_window(start, end, limit=max(limit * 6, 40))
        sample_episodes = [
            {
                "item_id": str(item.get("id") or ""),
                "title": item.get("title") or item.get("filename") or item.get("id"),
                "timestamp": _best_timestamp(item).isoformat(),
                "source_type": item.get("source_type"),
            }
            for item in episodes[:limit]
        ]
        return {
            "mode": "activity",
            "topic": topic,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "entities": [
                {
                    "entity": entity["name"],
                    "type": entity["entity_type"],
                    "episode_count": int(entity.get("period_mentions") or 0),
                    "sample_episodes": [
                        {
                            "item_id": str(item.get("id") or ""),
                            "title": item.get("title") or item.get("filename") or item.get("id"),
                            "timestamp": _best_timestamp(item).isoformat(),
                            "source_type": item.get("source_type"),
                        }
                        for item in self._load_items_by_ids(
                            [
                                str(entry.get("id") or "")
                                for entry in self.graph_store.get_episodes_mentioning(
                                    str(entity.get("id") or ""),
                                    limit=3,
                                )
                            ]
                        )[:3]
                    ]
                    or sample_episodes[:3],
                }
                for entity in filtered_entities[:limit]
            ],
            "episodes": sample_episodes,
        }

    def state_mode(
        self,
        entity_name: str,
        as_of: datetime,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        entity = self.graph_store.get_entity_by_name(entity_name)
        if entity is None:
            matches = self.graph_store.search_entities(entity_name, limit=1)
            entity = matches[0] if matches else None
        if entity is None:
            return {
                "mode": "state",
                "entity": None,
                "as_of": as_of.isoformat(),
                "state": {},
                "facts": [],
                "evidence": [],
            }

        facts = self.graph_store.get_facts_at_time(str(entity["id"]), as_of)
        evidence_items = self._load_items_by_ids([str(fact["episode_id"]) for fact in facts])
        state: dict[str, list[str]] = {}
        for fact in facts:
            predicate = str(fact["predicate"])
            entity_role = str(fact.get("entity_role") or "subject")
            counterpart = (
                str(fact["object"])
                if entity_role == "subject"
                else str(fact["subject"])
            )
            state.setdefault(predicate, []).append(counterpart)
        return {
            "mode": "state",
            "entity": entity,
            "as_of": as_of.isoformat(),
            "state": state,
            "facts": facts[:limit],
            "evidence": [
                {
                    "item_id": str(item.get("id") or ""),
                    "title": item.get("title") or item.get("filename") or item.get("id"),
                    "timestamp": _best_timestamp(item).isoformat(),
                }
                for item in evidence_items[:limit]
            ],
        }

    def document_mode(
        self,
        start: datetime,
        end: datetime,
        *,
        topic: str | None = None,
        parsed: query_parser.ParsedQuery | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        working_parsed = parsed or query_parser.parse_query(topic or "")
        candidates = self._load_items_in_window(start, end, limit=max(limit * 8, 50))
        ranked_items: list[dict[str, Any]] = []
        for item in candidates:
            content_score = retrieval_search._content_score(item, working_parsed)
            filename_score = retrieval_search._filename_score(item, working_parsed)
            metadata_score = retrieval_search._metadata_match(item, working_parsed)
            time_score = retrieval_search._time_fit(
                item,
                query_parser.TimeWindow(
                    description="document_mode",
                    start_date=start,
                    end_date=end,
                    center_date=start + (end - start) / 2,
                    size_days=max(1, (end - start).days),
                ),
            )
            lexical_score = (
                (content_score * 0.55)
                + (filename_score * 0.20)
                + (metadata_score * 0.10)
                + (time_score * 0.15)
            )
            if not str(working_parsed.semantic_text or "").strip():
                lexical_score = time_score
            ranked_items.append({"item": item, "score": round(float(lexical_score), 4)})
        ranked_items.sort(
            key=lambda entry: (
                float(entry["score"]),
                _best_timestamp(entry["item"]),
            ),
            reverse=True,
        )
        return {
            "mode": "document",
            "topic": topic,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "episodes": [
                {
                    "item_id": str(entry["item"].get("id") or ""),
                    "title": entry["item"].get("title") or entry["item"].get("filename") or entry["item"].get("id"),
                    "source_type": entry["item"].get("source_type"),
                    "file_type": entry["item"].get("file_type"),
                    "timestamp": _best_timestamp(entry["item"]).isoformat(),
                    "score": float(entry["score"]),
                }
                for entry in ranked_items[:limit]
            ],
        }

    def query_period(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        parsed = query_parser.parse_query(query)
        time_window = self._resolve_window(query)
        start = time_window.start_date or (datetime.now(timezone.utc) - timedelta(days=30))
        end = time_window.end_date or datetime.now(timezone.utc)
        anchor = self._resolve_entity_anchor(parsed)

        if parsed.intent_class == query_parser.IntentClass.TEMPORAL_STATE.value and anchor is not None:
            payload = self.state_mode(str(anchor["name"]), end, limit=limit)
        elif parsed.intent_class == query_parser.IntentClass.TEMPORAL_RANGE.value and anchor is not None:
            if self._prefers_document_mode(parsed):
                payload = self.document_mode(start, end, topic=str(anchor["name"]), parsed=parsed, limit=limit)
            else:
                payload = self.activity_mode(start, end, topic=str(anchor["name"]), limit=limit)
        elif parsed.intent_class == query_parser.IntentClass.TEMPORAL_RANGE.value:
            if self._prefers_document_mode(parsed):
                payload = self.document_mode(start, end, topic=None, parsed=parsed, limit=limit)
            else:
                payload = self.activity_mode(start, end, topic=parsed.semantic_text, limit=limit)
        else:
            payload = self.activity_mode(start, end, topic=str(anchor["name"]) if anchor is not None else parsed.semantic_text, limit=limit)

        return {
            "query": query,
            "intent_class": parsed.intent_class,
            "time_window": {
                "description": time_window.description,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "size_days": int(time_window.size_days),
            },
            **payload,
        }

    def get_entity_timeline(self, entity_name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.graph_store.get_entity_timeline(entity_name, limit=limit)

    def get_idea_evolution(self, topic: str, *, limit: int = 20) -> dict[str, Any]:
        entity = self.graph_store.get_entity_by_name(topic)
        if entity is None:
            matches = self.graph_store.search_entities(topic, limit=1)
            entity = matches[0] if matches else None
        if entity is None:
            return {
                "entity": None,
                "timeline": [],
                "episodes": [],
            }

        mentions = self.graph_store.get_episode_mentions(str(entity["name"]), limit=max(limit * 4, 20))
        items = self._load_items_by_ids([str(mention["episode_id"]) for mention in mentions])
        items.sort(key=_best_timestamp)

        timeline = self.graph_store.get_entity_timeline(str(entity["name"]), limit=limit * 2)
        return {
            "entity": entity,
            "timeline": timeline[:limit],
            "episodes": [
                {
                    "item_id": str(item.get("id") or ""),
                    "title": item.get("title") or item.get("filename") or item.get("id"),
                    "timestamp": _best_timestamp(item).isoformat(),
                    "source_type": item.get("source_type"),
                    "file_type": item.get("file_type"),
                }
                for item in items[:limit]
            ],
        }
