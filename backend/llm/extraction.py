from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

try:
    from ..cognition.heuristics import (
        FRAME_REGISTRY,
        extract_entities_from_item,
        extract_graph_entities_from_item,
        infer_frame,
    )
    from ..cognition.store import CognitiveStore
    from ..graph.facts import choose_anchor_time, extract_temporal_fact_specs, guess_fact_entity_type
    from ..graph.store import GraphStore, get_store as get_graph_store, normalize_entity_key
    from .local_llm import LocalLLM
    from .prompts import ENTITY_EXTRACTION_PROMPT, FRAME_EXTRACTION_PROMPT
except ImportError:
    from cognition.heuristics import (
        FRAME_REGISTRY,
        extract_entities_from_item,
        extract_graph_entities_from_item,
        infer_frame,
    )
    from cognition.store import CognitiveStore
    from graph.facts import choose_anchor_time, extract_temporal_fact_specs, guess_fact_entity_type
    from graph.store import GraphStore, get_store as get_graph_store, normalize_entity_key
    from llm.local_llm import LocalLLM
    from llm.prompts import ENTITY_EXTRACTION_PROMPT, FRAME_EXTRACTION_PROMPT


logger = logging.getLogger(__name__)

INGESTION_CREATED_EVENT = "ingestion:created"
INGESTION_ENTITY_ITEM_EDGE = "entity_item"
INGESTION_COOCCURRENCE_EDGE_PREFIX = "co_occurrence:item:"

_ENTITY_TYPE_MAP = {
    "person": "Person",
    "people": "Person",
    "project": "Project",
    "topic": "Concept",
    "concept": "Concept",
    "organisation": "Organisation",
    "organization": "Organisation",
    "place": "Place",
    "document": "Document",
}


def _strip_json_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _load_json_payload(text: str) -> dict[str, Any]:
    payload = json.loads(_strip_json_fences(text))
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _parse_keywords(raw_keywords: Any) -> list[str]:
    if raw_keywords is None:
        return []
    if isinstance(raw_keywords, list):
        return [str(entry).strip() for entry in raw_keywords if str(entry).strip()]

    text = str(raw_keywords).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(entry).strip() for entry in parsed if str(entry).strip()]

    return [
        entry.strip()
        for entry in re.split(r"[,;\n]+", text)
        if entry.strip()
    ]


def _merge_keywords(*groups: list[str], limit: int = 18) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            keyword = " ".join(str(value or "").split()).strip()
            if not keyword:
                continue
            normalized = keyword.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(keyword)
            if len(merged) >= limit:
                return merged
    return merged


def _build_content(item: dict[str, Any], *, limit_chars: int = 4000) -> str:
    parts = [
        f"Title: {item.get('title') or item.get('filename') or ''}",
        f"Filename: {item.get('filename') or ''}",
        f"Source: {item.get('source_type') or 'local'}",
        f"Summary: {item.get('summary') or ''}",
        f"Preview: {item.get('preview_text') or ''}",
        f"Keywords: {', '.join(_parse_keywords(item.get('keywords')))}",
        f"Content: {str(item.get('extracted_text') or '')[:limit_chars]}",
    ]
    return "\n".join(part for part in parts if part and not part.endswith(": "))


def _normalize_entity_type(value: str | None, *, default: str = "Concept") -> str:
    normalized = str(value or "").strip().casefold()
    return _ENTITY_TYPE_MAP.get(normalized, default)


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [" ".join(str(entry).split()).strip() for entry in value if str(entry).strip()]
    text = " ".join(str(value).split()).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [" ".join(str(entry).split()).strip() for entry in parsed if str(entry).strip()]
    return [
        entry.strip()
        for entry in re.split(r",|/|;|\n| and ", text)
        if entry.strip()
    ]


def _coerce_slot_value(frame_type: str, slot_name: str, value: Any) -> Any:
    slot_spec = FRAME_REGISTRY.get(frame_type, {}).get("slots", {}).get(slot_name, {})
    slot_type = str(slot_spec.get("type") or "text")
    if slot_type == "list":
        return _coerce_list(value)

    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text:
        return None

    if slot_type == "status":
        allowed = [str(entry).casefold() for entry in slot_spec.get("values", [])]
        normalized = text.casefold()
        for entry in allowed:
            if normalized == entry or entry in normalized:
                return entry
    return text


def _merge_frame_slots(
    frame_type: str,
    heuristic_slots: dict[str, Any],
    llm_slots: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(heuristic_slots)
    if not llm_slots:
        return merged

    for slot_name in FRAME_REGISTRY.get(frame_type, {}).get("slots", {}):
        if slot_name not in llm_slots:
            continue
        value = _coerce_slot_value(frame_type, slot_name, llm_slots.get(slot_name))
        if value is None or value == []:
            continue
        if isinstance(value, list):
            merged[slot_name] = _merge_keywords(
                [str(entry) for entry in merged.get(slot_name, []) if str(entry).strip()],
                [str(entry) for entry in value if str(entry).strip()],
                limit=8,
            )
        else:
            merged[slot_name] = value
    return merged


def _keyword_candidates_from_slots(frame_type: str, slots: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for slot_name in FRAME_REGISTRY.get(frame_type, {}).get("slots", {}):
        value = slots.get(slot_name)
        if isinstance(value, list):
            candidates.extend(str(entry) for entry in value[:4])
        elif isinstance(value, str):
            candidates.append(value)
    return [candidate for candidate in candidates if str(candidate).strip()]


def _ensure_entity(
    entities: dict[str, dict[str, Any]],
    *,
    name: str | None,
    entity_type: str,
    confidence: float,
    aliases: list[str] | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    cleaned_name = " ".join(str(name or "").split()).strip()
    key = normalize_entity_key(cleaned_name)
    if not cleaned_name or not key:
        return

    candidate_aliases = [
        alias
        for alias in dict.fromkeys(
            [cleaned_name, *(str(alias).strip() for alias in (aliases or []) if str(alias).strip())]
        )
        if alias
    ]
    candidate_attributes = dict(attributes or {})
    if "@" in cleaned_name and "email" not in candidate_attributes:
        candidate_attributes["email"] = cleaned_name.casefold()

    existing = entities.get(key)
    if existing is None:
        entities[key] = {
            "name": cleaned_name,
            "entity_type": _normalize_entity_type(entity_type, default=guess_fact_entity_type(cleaned_name)),
            "aliases": candidate_aliases,
            "attributes": candidate_attributes,
            "confidence": max(0.0, min(1.0, float(confidence))),
        }
        return

    existing_confidence = float(existing.get("confidence") or 0.0)
    if float(confidence) >= existing_confidence and len(cleaned_name) >= len(str(existing.get("name") or "")):
        existing["name"] = cleaned_name
    if str(existing.get("entity_type") or "Concept") == "Concept":
        existing["entity_type"] = _normalize_entity_type(entity_type, default=existing["entity_type"])
    existing["aliases"] = _merge_keywords(
        [str(alias) for alias in existing.get("aliases", [])],
        candidate_aliases,
        limit=8,
    )
    existing["attributes"] = {**dict(existing.get("attributes") or {}), **candidate_attributes}
    existing["confidence"] = max(existing_confidence, max(0.0, min(1.0, float(confidence))))


def _relationship_predicate(value: Any) -> str:
    normalized = re.sub(r"[^A-Z0-9_]+", "_", str(value or "").upper()).strip("_")
    return normalized or "RELATES_TO"


async def _extract_llm_entities(
    item: dict[str, Any],
    llm: LocalLLM,
) -> dict[str, Any] | None:
    prompt = ENTITY_EXTRACTION_PROMPT.format(
        source_type=item.get("source_type") or "local",
        content=_build_content(item),
    )
    response = await llm.generate(prompt, max_tokens=560, temperature=0.0)
    payload = _load_json_payload(response)

    entities: list[dict[str, Any]] = []
    for raw_entity in payload.get("entities", []):
        name = " ".join(str((raw_entity or {}).get("name") or "").split()).strip()
        if not name:
            continue
        confidence = max(0.0, min(1.0, float((raw_entity or {}).get("confidence", 0.0) or 0.0)))
        if confidence < 0.55:
            continue
        entities.append(
            {
                "name": name,
                "entity_type": _normalize_entity_type((raw_entity or {}).get("type"), default=guess_fact_entity_type(name)),
                "confidence": confidence,
            }
        )

    relationships: list[dict[str, Any]] = []
    for raw_relationship in payload.get("relationships", []):
        subject = " ".join(str((raw_relationship or {}).get("subject") or "").split()).strip()
        object_name = " ".join(str((raw_relationship or {}).get("object") or "").split()).strip()
        if not subject or not object_name or subject == object_name:
            continue
        confidence = max(0.0, min(1.0, float((raw_relationship or {}).get("confidence", 0.0) or 0.0)))
        if confidence < 0.55:
            continue
        relationships.append(
            {
                "subject": subject,
                "predicate": _relationship_predicate((raw_relationship or {}).get("predicate")),
                "object": object_name,
                "confidence": confidence,
            }
        )
    return {"entities": entities, "relationships": relationships}


async def _extract_llm_frame_slots(
    item: dict[str, Any],
    *,
    frame_type: str,
    llm: LocalLLM,
) -> dict[str, Any] | None:
    if frame_type not in FRAME_REGISTRY:
        return None
    prompt = FRAME_EXTRACTION_PROMPT.format(
        frame_type=frame_type.replace("_", " "),
        content=_build_content(item),
        slots_json=json.dumps(
            FRAME_REGISTRY[frame_type]["slots"],
            ensure_ascii=True,
            sort_keys=True,
        ),
    )
    response = await llm.generate(prompt, max_tokens=420, temperature=0.0)
    payload = _load_json_payload(response)
    return payload


async def extract_item_knowledge(
    item: dict[str, Any],
    *,
    allow_model: bool = True,
    llm: LocalLLM | None = None,
) -> dict[str, Any]:
    frame_type, heuristic_slots, heuristic_confidence = infer_frame(item)
    heuristic_graph_entities = extract_graph_entities_from_item(item)
    heuristic_cognitive_entities = extract_entities_from_item(item)
    anchor_time = choose_anchor_time(item, datetime.now(timezone.utc))

    merged_entities: dict[str, dict[str, Any]] = {}
    for entity in heuristic_graph_entities:
        _ensure_entity(
            merged_entities,
            name=entity.get("name"),
            entity_type=str(entity.get("entity_type") or "Concept"),
            confidence=float(entity.get("confidence", 0.65) or 0.65),
            aliases=list(entity.get("aliases") or []),
            attributes=dict(entity.get("attributes") or {}),
        )

    provider = "heuristic"
    llm_relationships: list[dict[str, Any]] = []
    merged_slots = dict(heuristic_slots)
    frame_confidence = float(heuristic_confidence or 0.0)

    model = llm or LocalLLM("standard")
    if allow_model and bool(model.status().get("available")):
        try:
            llm_entities = await _extract_llm_entities(item, model)
        except Exception as exc:
            logger.debug("LLM entity extraction failed for %s: %s", item.get("id"), exc)
            llm_entities = None

        if llm_entities is not None:
            provider = "llm"
            for entity in llm_entities.get("entities", []):
                _ensure_entity(
                    merged_entities,
                    name=entity.get("name"),
                    entity_type=str(entity.get("entity_type") or "Concept"),
                    confidence=float(entity.get("confidence", 0.75) or 0.75),
                )
            llm_relationships = list(llm_entities.get("relationships") or [])

        if frame_type in FRAME_REGISTRY:
            try:
                llm_slots = await _extract_llm_frame_slots(item, frame_type=frame_type, llm=model)
            except Exception as exc:
                logger.debug("LLM frame extraction failed for %s: %s", item.get("id"), exc)
                llm_slots = None
            if llm_slots is not None:
                provider = "llm"
                merged_slots = _merge_frame_slots(frame_type, merged_slots, llm_slots)
                frame_confidence = max(frame_confidence, 0.92)

    temporal_facts = extract_temporal_fact_specs(
        item,
        frame_type=frame_type,
        slots=merged_slots,
        anchor_time=anchor_time,
    )

    seen_facts: set[tuple[str, str, str]] = set()
    for fact in list(temporal_facts):
        seen_facts.add(
            (
                normalize_entity_key(fact["subject"]),
                str(fact["predicate"]),
                normalize_entity_key(fact["object"]),
            )
        )
        _ensure_entity(
            merged_entities,
            name=fact["subject"],
            entity_type=fact["subject_type"],
            confidence=float(fact.get("confidence", 0.7) or 0.7),
        )
        _ensure_entity(
            merged_entities,
            name=fact["object"],
            entity_type=fact["object_type"],
            confidence=float(fact.get("confidence", 0.7) or 0.7),
        )

    for relationship in llm_relationships:
        _ensure_entity(
            merged_entities,
            name=relationship["subject"],
            entity_type=guess_fact_entity_type(relationship["subject"]),
            confidence=float(relationship.get("confidence", 0.7) or 0.7),
        )
        _ensure_entity(
            merged_entities,
            name=relationship["object"],
            entity_type=guess_fact_entity_type(relationship["object"]),
            confidence=float(relationship.get("confidence", 0.7) or 0.7),
        )
        fact_key = (
            normalize_entity_key(relationship["subject"]),
            relationship["predicate"],
            normalize_entity_key(relationship["object"]),
        )
        if not all(fact_key) or fact_key in seen_facts:
            continue
        temporal_facts.append(
            {
                "subject": relationship["subject"],
                "subject_type": guess_fact_entity_type(relationship["subject"]),
                "predicate": relationship["predicate"],
                "object": relationship["object"],
                "object_type": guess_fact_entity_type(relationship["object"]),
                "valid_from": anchor_time,
                "episode_id": str(item.get("id") or ""),
                "confidence": float(relationship.get("confidence", 0.7) or 0.7),
            }
        )
        seen_facts.add(fact_key)

    cognitive_scores = {
        entity_id: float(score)
        for entity_id, score in heuristic_cognitive_entities
    }
    for entity in merged_entities.values():
        entity_name = str(entity.get("name") or "").strip()
        entity_id = f"entity:{_normalize_token(entity_name)}"
        if entity_id == "entity:":
            continue
        confidence = max(0.3, min(1.0, 0.25 + float(entity.get("confidence", 0.65) or 0.65)))
        cognitive_scores[entity_id] = max(cognitive_scores.get(entity_id, 0.0), confidence)

    graph_entities = sorted(
        merged_entities.values(),
        key=lambda entity: (
            float(entity.get("confidence") or 0.0),
            len(str(entity.get("name") or "")),
        ),
        reverse=True,
    )[:14]

    keywords = _merge_keywords(
        _parse_keywords(item.get("keywords")),
        [frame_type.replace("_", " ")] if frame_type not in {"document", "visual_reference"} else [],
        [str(entity.get("name") or "") for entity in graph_entities[:10]],
        _keyword_candidates_from_slots(frame_type, merged_slots),
        limit=18,
    )

    cognitive_entities = sorted(
        cognitive_scores.items(),
        key=lambda entry: (entry[1], len(entry[0])),
        reverse=True,
    )[:14]

    return {
        "provider": provider,
        "frame": {
            "frame_type": frame_type,
            "slots": merged_slots,
            "confidence": round(frame_confidence or heuristic_confidence, 4),
        },
        "graph_entities": graph_entities,
        "cognitive_entities": cognitive_entities,
        "temporal_facts": temporal_facts,
        "keywords": keywords,
        "anchor_time": anchor_time.isoformat(),
    }


def apply_item_knowledge(
    item: dict[str, Any],
    knowledge: dict[str, Any],
    *,
    graph_store: GraphStore | None = None,
    cognitive_store: CognitiveStore | None = None,
) -> dict[str, Any]:
    item_id = str(item.get("id") or "")
    if not item_id:
        return {"item_id": "", "frame_type": None, "entities": 0, "temporal_facts": 0, "provider": "heuristic"}

    graph = graph_store or get_graph_store()
    cognitive = cognitive_store or CognitiveStore()
    frame = dict(knowledge.get("frame") or {})
    graph_entities = list(knowledge.get("graph_entities") or [])
    cognitive_entities = list(knowledge.get("cognitive_entities") or [])
    temporal_facts = list(knowledge.get("temporal_facts") or [])
    provider = str(knowledge.get("provider") or "heuristic")
    anchor_time = str(knowledge.get("anchor_time") or choose_anchor_time(item, datetime.now(timezone.utc)).isoformat())

    cooccurrence_edge_type = f"{INGESTION_COOCCURRENCE_EDGE_PREFIX}{item_id}"
    cognitive.clear_item_knowledge(item_id, extra_edge_types=[cooccurrence_edge_type])
    cognitive.upsert_frame_instance(
        item_id,
        str(frame.get("frame_type") or "document"),
        dict(frame.get("slots") or {}),
        confidence=float(frame.get("confidence", 0.0) or 0.0),
    )

    if not cognitive.has_activation_history(item_id):
        cognitive.record_activation_event(
            item_id,
            INGESTION_CREATED_EVENT,
            event_weight=0.25,
            occurred_at=anchor_time,
            metadata={"source": "ingestion", "provider": provider},
        )

    edge_rows: list[tuple[str, str, float, str]] = []
    item_node = f"item:{item_id}"
    for entity_id, weight in cognitive_entities:
        bounded_weight = max(0.1, min(1.0, float(weight)))
        edge_rows.append((str(entity_id), item_node, bounded_weight, INGESTION_ENTITY_ITEM_EDGE))
        edge_rows.append((item_node, str(entity_id), bounded_weight, INGESTION_ENTITY_ITEM_EDGE))

    for index, (left_id, left_weight) in enumerate(cognitive_entities):
        for right_id, right_weight in cognitive_entities[index + 1:]:
            pair_weight = max(0.1, min(1.0, (float(left_weight) + float(right_weight)) / 2.0))
            edge_rows.append((str(left_id), str(right_id), pair_weight, cooccurrence_edge_type))
            edge_rows.append((str(right_id), str(left_id), pair_weight, cooccurrence_edge_type))

    if edge_rows:
        cognitive.bulk_upsert_entity_edges(edge_rows)

    graph.clear_episode_state(item_id)
    entity_id_cache: dict[str, str] = {}
    with graph.get_connection() as conn:
        for entity in graph_entities:
            entity_name = str(entity.get("name") or "").strip()
            if not entity_name:
                continue
            entity_id = graph._upsert_entity_conn(
                conn,
                name=entity_name,
                entity_type=str(entity.get("entity_type") or guess_fact_entity_type(entity_name)),
                aliases=list(entity.get("aliases") or []),
                attributes=dict(entity.get("attributes") or {}),
            )
            entity_id_cache[normalize_entity_key(entity_name)] = entity_id
            graph._link_episode_to_entity_conn(
                conn,
                episode_id=item_id,
                entity_id=entity_id,
                mentioned_at=anchor_time,
                confidence=float(entity.get("confidence", 0.7) or 0.7),
            )

        def ensure_entity_id(name: str, entity_type: str) -> str:
            key = normalize_entity_key(name)
            existing_id = entity_id_cache.get(key)
            if existing_id is not None:
                return existing_id
            entity_id = graph._upsert_entity_conn(
                conn,
                name=name,
                entity_type=entity_type,
            )
            entity_id_cache[key] = entity_id
            return entity_id

        for fact in temporal_facts:
            subject = " ".join(str(fact.get("subject") or "").split()).strip()
            object_name = " ".join(str(fact.get("object") or "").split()).strip()
            if not subject or not object_name or subject == object_name:
                continue
            subject_id = ensure_entity_id(subject, str(fact.get("subject_type") or guess_fact_entity_type(subject)))
            object_id = ensure_entity_id(object_name, str(fact.get("object_type") or guess_fact_entity_type(object_name)))
            if subject_id == object_id:
                continue
            graph._add_temporal_fact_conn(
                conn,
                subject_id=subject_id,
                predicate=_relationship_predicate(fact.get("predicate")),
                object_id=object_id,
                valid_from=str(fact.get("valid_from") or anchor_time),
                episode_id=item_id,
                confidence=float(fact.get("confidence", 0.7) or 0.7),
            )

    enrichment_summary: dict[str, Any] = {}
    try:
        try:
            from ..graph.enrichment import enrich_item_graph_context
        except ImportError:
            from graph.enrichment import enrich_item_graph_context

        enrichment_summary = enrich_item_graph_context(
            item,
            graph_store=graph,
        )
    except Exception:
        logger.exception("Graph context enrichment failed for %s", item_id)

    return {
        "item_id": item_id,
        "frame_type": str(frame.get("frame_type") or "document"),
        "entities": len(graph_entities),
        "cognitive_entities": len(cognitive_entities),
        "temporal_facts": len(temporal_facts),
        "provider": provider,
        "graph_context": enrichment_summary,
    }
