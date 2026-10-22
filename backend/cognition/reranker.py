from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

try:
    from ..graph.store import GraphStore, get_store as get_graph_store
    from ..retrieval import query_parser
except ImportError:
    from graph.store import GraphStore, get_store as get_graph_store
    from retrieval import query_parser

from .activation import compute_base_level, normalize_activation
from .heuristics import extract_query_entities, infer_desired_frame
from .scripts.detector import compute_script_relevance, detect_script_context
from .spreading import SpreadingActivationEngine
from .store import CognitiveStore, get_store


if TYPE_CHECKING:
    from ..retrieval.search import SearchResult


logger = logging.getLogger(__name__)
USE_DYNAMIC_GRAPH_WEIGHTING = True
USE_GRAPH_RERANK = True

WEIGHTS = {
    "recurdo_base": 0.40,
    "base_level": 0.25,
    "spreading": 0.20,
    "frame_match": 0.10,
    "script_relevance": 0.05,
    "graph_relevance": 0.05,
}
GRAPH_BASELINE_WEIGHT = 0.05
EXPANDED_QUERY_ENTITY_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "how",
    "major",
    "project",
    "projects",
    "related",
    "that",
    "the",
    "thing",
    "things",
    "this",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "work",
    "worked",
    "working",
}
GMAIL_SOURCE_TYPES = {"gmail", "gmail_attachment"}
GMAIL_QUERY_SOURCE_TYPES = {"email", "gmail", "gmail_attachment"}
PROJECT_RECALL_QUERY_TERMS = {
    "project",
    "projects",
    "startup",
    "venture",
    "working",
    "worked",
    "work",
}
PROJECT_EXECUTION_META_PATTERNS = (
    re.compile(r"\bnext\s+steps?\b", re.IGNORECASE),
    re.compile(r"\breal\s+next\s+work\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+here\s+to\s+(?:real\s+)?users\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s\s+real\b.*\bwhat'?s\s+next\b", re.IGNORECASE),
    re.compile(r"\bproduction\s+readiness\b", re.IGNORECASE),
    re.compile(r"\baudit(?:\s+fix)?\s+prompts?\b", re.IGNORECASE),
)


def _normalized_weights(
    available: dict[str, bool],
    *,
    weight_map: dict[str, float] | None = None,
) -> dict[str, float]:
    source_weights = weight_map or WEIGHTS
    total = sum(weight for name, weight in source_weights.items() if available.get(name))
    if total <= 0:
        return {"recurdo_base": 1.0}
    return {
        name: weight / total
        for name, weight in source_weights.items()
        if available.get(name)
    }


def rerank_results(
    recurdo_results: list["SearchResult"],
    query: str,
    *,
    cognitive_store: CognitiveStore | None = None,
    graph_store: GraphStore | None = None,
) -> list["SearchResult"]:
    if not recurdo_results:
        return recurdo_results

    try:
        parsed_query = query_parser.parse_query(query)
    except Exception:
        parsed_query = None

    store = cognitive_store or get_store()
    now = datetime.now(timezone.utc)
    item_ids = [result.item_id for result in recurdo_results if result.item_id]

    histories = store.get_activation_histories(item_ids)
    base_level_scores = {
        item_id: normalize_activation(compute_base_level(history, current_time=now))
        for item_id, history in histories.items()
    }

    query_entities = _expanded_query_entities(query)
    spreading_scores: dict[str, float] = {}
    if query_entities:
        engine = SpreadingActivationEngine(store)
        spreading_scores = engine.spread({entity_id: 1.0 for entity_id in query_entities})

    desired_frame = infer_desired_frame(query)
    frames = store.get_frame_instances(item_ids)
    script_context = detect_script_context(
        store,
        current_query=query,
        now=now,
    )
    graph_target = GRAPH_BASELINE_WEIGHT
    if parsed_query is not None:
        intent_profile = query_parser.QueryIntentClassifier.get_weights(
            getattr(parsed_query, "intent_class", query_parser.IntentClass.SEMANTIC_TOPIC.value)
        )
        graph_target = float(intent_profile.get("graph", GRAPH_BASELINE_WEIGHT) or GRAPH_BASELINE_WEIGHT)
    graph_details = {
        "scores": {},
        "seed_entities": [],
        "direct_matches": {},
        "neighbor_matches": {},
    }
    should_run_graph = bool(
        USE_GRAPH_RERANK
        and (
            graph_target > 0.09
            or (graph_target >= 0.05 and bool(query_entities))
        )
    )
    if should_run_graph:
        try:
            graph = graph_store or get_graph_store()
            graph_details = graph.score_items_for_query_entities(query_entities, item_ids)
        except Exception:
            logger.exception("Graph-assisted rerank failed for query=%r", query)

    any_base_level = any(score > 0 for score in base_level_scores.values())
    any_spreading = any(score > 0 for score in spreading_scores.values())
    any_frame = bool(desired_frame) and any(frame for frame in frames.values())
    any_script = bool(script_context.get("active_script"))
    any_graph = any(float(score) > 0 for score in graph_details.get("scores", {}).values())
    weight_map = dict(WEIGHTS)
    if USE_DYNAMIC_GRAPH_WEIGHTING and parsed_query is not None:
        graph_scale = max(0.25, min(graph_target / GRAPH_BASELINE_WEIGHT, 8.0))
        weight_map["graph_relevance"] *= graph_scale
        if getattr(parsed_query, "intent_class", "") in {
            query_parser.IntentClass.EXACT_LOOKUP.value,
            query_parser.IntentClass.SEMANTIC_TOPIC.value,
        }:
            weight_map["spreading"] *= 0.75
        if getattr(parsed_query, "intent_class", "") == query_parser.IntentClass.TEMPORAL_STATE.value:
            weight_map["frame_match"] *= 1.2

    weights = _normalized_weights(
        {
            "recurdo_base": True,
            "base_level": any_base_level,
            "spreading": any_spreading,
            "frame_match": any_frame,
            "script_relevance": any_script,
            "graph_relevance": any_graph,
        },
        weight_map=weight_map,
    )

    scored: list["SearchResult"] = []
    for result in recurdo_results:
        frame = frames.get(result.item_id)
        frame_score = 0.0
        if desired_frame and frame:
            if frame.get("frame_type") == desired_frame:
                frame_score = float(frame.get("confidence") or 0.0)
            else:
                frame_score = min(0.3, float(frame.get("confidence") or 0.0) * 0.3)

        recurdo_score = float(result.final_score)
        item_spreading = spreading_scores.get(f"item:{result.item_id}", 0.0)
        item_base_level = base_level_scores.get(result.item_id, 0.0)
        script_score = compute_script_relevance(
            frame_type=frame.get("frame_type") if frame else None,
            item=result.item,
            script_context=script_context,
        )
        graph_score = float(graph_details.get("scores", {}).get(result.item_id, 0.0) or 0.0)

        final_score = (
            weights.get("recurdo_base", 0.0) * recurdo_score
            + weights.get("base_level", 0.0) * item_base_level
            + weights.get("spreading", 0.0) * item_spreading
            + weights.get("frame_match", 0.0) * frame_score
            + weights.get("script_relevance", 0.0) * script_score
            + weights.get("graph_relevance", 0.0) * graph_score
        )
        if result.filename_score >= 0.95:
            final_score += 0.12 if getattr(parsed_query, "query_style", "") == "identifier" else 0.08
        elif getattr(parsed_query, "query_style", "") == "identifier" and result.filename_score >= 0.85:
            final_score += 0.04
        final_score *= _source_type_guardrail(result.item, parsed_query)
        project_meta_guardrail = _project_execution_meta_guardrail(result.item, parsed_query)
        final_score *= project_meta_guardrail

        result.base_score = recurdo_score
        result.cognitive_score = final_score
        result.final_score = final_score
        result.frame_type = frame.get("frame_type") if frame else None
        result.cognitive_breakdown = {
            "recurdo_base": round(recurdo_score, 3),
            "base_level": round(item_base_level, 3),
            "spreading": round(item_spreading, 3),
            "frame_match": round(frame_score, 3),
            "script_relevance": round(script_score, 3),
            "graph_relevance": round(graph_score, 3),
            "weights": {name: round(value, 3) for name, value in weights.items()},
            "query_entities": query_entities,
            "desired_frame": desired_frame,
            "graph_seed_entities": graph_details.get("seed_entities", []),
            "graph_direct_matches": graph_details.get("direct_matches", {}).get(result.item_id, []),
            "graph_neighbor_matches": graph_details.get("neighbor_matches", {}).get(result.item_id, []),
            "active_script": script_context.get("active_script"),
            "current_scene": script_context.get("current_scene"),
            "query_scene": script_context.get("query_scene"),
            "source_type_guardrail": round(_source_type_guardrail(result.item, parsed_query), 3),
            "project_meta_guardrail": round(project_meta_guardrail, 3),
            "final": round(final_score, 3),
        }
        scored.append(result)

    scored.sort(key=lambda item: (item.final_score, item.base_score), reverse=True)
    return scored


def _source_type_guardrail(item: dict[str, Any], parsed_query: Any | None) -> float:
    source_type = str(item.get("source_type") or "").strip().casefold()
    if source_type not in GMAIL_SOURCE_TYPES:
        return 1.0

    parsed_source = str(getattr(parsed_query, "source_type", "") or "").strip().casefold()
    if parsed_source in GMAIL_QUERY_SOURCE_TYPES:
        return 1.0

    # Search already downranks Gmail for generic queries. Re-apply a final
    # cognitive-stage guardrail so graph/spreading activation cannot lift
    # imported emails or attachments back above local files.
    return 0.55 if source_type == "gmail_attachment" else 0.65


def _project_execution_meta_guardrail(item: dict[str, Any], parsed_query: Any | None) -> float:
    if parsed_query is None:
        return 1.0

    query_text = str(getattr(parsed_query, "original", "") or "")
    query_tokens = {token.casefold() for token in re.findall(r"[A-Za-z0-9]+", query_text)}
    if not (
        query_tokens & PROJECT_RECALL_QUERY_TERMS
        or re.search(r"\b(?:working\s+on|worked\s+on|building|built|making|made)\b", query_text, re.IGNORECASE)
    ):
        return 1.0

    if any(pattern.search(query_text) for pattern in PROJECT_EXECUTION_META_PATTERNS):
        return 1.0

    searchable = " ".join(
        filter(
            None,
            [
                str(item.get("title") or ""),
                str(item.get("filename") or ""),
                str(item.get("original_path") or ""),
            ],
        )
    )
    if any(pattern.search(searchable) for pattern in PROJECT_EXECUTION_META_PATTERNS):
        return 0.45
    return 1.0


def _expanded_query_entities(query: str, *, limit: int = 10) -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()

    def add(entity_id: str) -> None:
        if entity_id == "entity:" or entity_id in seen:
            return
        seen.add(entity_id)
        entities.append(entity_id)

    for entity_id in extract_query_entities(query, limit=limit):
        add(entity_id)

    try:
        parsed = query_parser.parse_query(query)
    except Exception:
        logger.debug("Unable to expand query entities for cognitive rerank", exc_info=True)
        return entities[:limit]

    for token in re.findall(r"[A-Za-z0-9]+", parsed.semantic_text or ""):
        normalized = token.casefold()
        if len(normalized) < 3:
            continue
        if normalized in EXPANDED_QUERY_ENTITY_STOPWORDS:
            continue
        add(f"entity:{normalized}")
        if len(entities) >= limit:
            break

    return entities[:limit]
