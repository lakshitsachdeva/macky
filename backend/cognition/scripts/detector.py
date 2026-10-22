from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
from typing import Any

from .library import SCRIPT_LIBRARY, humanize_script_name


GENERIC_SCRIPT_FRAME_TYPES = {"document", "email"}


def _normalize_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        cleaned = str(value).strip()
        if not cleaned:
            return None
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _recent_activity_items(cognitive_store, *, limit: int = 200) -> list[dict[str, Any]]:
    with cognitive_store.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT item_id, MAX(occurred_at) AS last_activity, COUNT(*) AS event_count
            FROM activation_history
            GROUP BY item_id
            ORDER BY last_activity DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    frame_map = cognitive_store.get_frame_instances([str(row["item_id"]) for row in rows])
    items: list[dict[str, Any]] = []
    for row in rows:
        item_id = str(row["item_id"])
        frame = frame_map.get(item_id)
        items.append(
            {
                "item_id": item_id,
                "last_activity": _normalize_datetime(str(row["last_activity"])),
                "event_count": int(row["event_count"] or 0),
                "frame_type": frame.get("frame_type") if frame else None,
            }
        )
    return items


def _build_text_blob(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    return " ".join(
        str(item.get(field) or "")
        for field in ("title", "filename", "summary", "preview_text", "extracted_text")
    ).casefold()


def detect_script_context(
    cognitive_store,
    *,
    current_frame_type: str | None = None,
    current_query: str | None = None,
    current_item: dict[str, Any] | None = None,
    now: datetime | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    reference_now = now or _utc_now()
    lowered_query = str(current_query or "").casefold()
    current_item_blob = _build_text_blob(current_item)
    recent_activity = _recent_activity_items(cognitive_store, limit=limit)

    script_scores: Counter[str] = Counter()
    scene_scores: dict[str, Counter[str]] = {
        script_name: Counter()
        for script_name in SCRIPT_LIBRARY
    }
    query_scene_scores: dict[str, Counter[str]] = {
        script_name: Counter()
        for script_name in SCRIPT_LIBRARY
    }
    last_activity_for_script: dict[str, datetime] = {}
    evidence: dict[str, list[str]] = {
        script_name: []
        for script_name in SCRIPT_LIBRARY
    }

    for entry in recent_activity:
        frame_type = entry.get("frame_type")
        last_activity = entry.get("last_activity")
        event_count = max(1, int(entry.get("event_count") or 1))
        if not frame_type or last_activity is None:
            continue
        if frame_type in GENERIC_SCRIPT_FRAME_TYPES:
            continue

        age_days = max(0.0, (reference_now - last_activity).total_seconds() / 86400.0)
        recency_weight = max(0.25, 1.0 - min(age_days, 45.0) / 60.0)
        activity_weight = min(1.8, 0.7 + math.log1p(event_count))
        score = recency_weight * activity_weight

        for script_name, script_def in SCRIPT_LIBRARY.items():
            for scene in script_def.get("scenes", []):
                if frame_type in scene.get("frame_types", []):
                    scene_scores[script_name][scene["name"]] += score
                    script_scores[script_name] += score
                    evidence[script_name].append(
                        f"recent {frame_type.replace('_', ' ')} activity supports {scene['name']}"
                    )
                    current_best = last_activity_for_script.get(script_name)
                    if current_best is None or last_activity > current_best:
                        last_activity_for_script[script_name] = last_activity

    if current_frame_type:
        for script_name, script_def in SCRIPT_LIBRARY.items():
            for scene in script_def.get("scenes", []):
                if current_frame_type in scene.get("frame_types", []):
                    scene_scores[script_name][scene["name"]] += 2.5
                    script_scores[script_name] += 2.5
                    evidence[script_name].append(
                        f"current frame matches {scene['name']}"
                    )

    for script_name, script_def in SCRIPT_LIBRARY.items():
        normalized_name = script_name.replace("_", " ")
        if normalized_name in lowered_query:
            script_scores[script_name] += 1.4
            evidence[script_name].append(f"query mentions {normalized_name}")

        for scene in script_def.get("scenes", []):
            for indicator in scene.get("indicators", []):
                indicator_l = str(indicator).casefold()
                if indicator_l in lowered_query:
                    query_scene_scores[script_name][scene["name"]] += 2.0
                    scene_scores[script_name][scene["name"]] += 2.0
                    script_scores[script_name] += 1.4
                    evidence[script_name].append(
                        f"query aligns with {scene['name']} via '{indicator}'"
                    )
                if indicator_l and indicator_l in current_item_blob:
                    scene_scores[script_name][scene["name"]] += 0.8
                    script_scores[script_name] += 0.4

    if not script_scores:
        return {
            "active_script": None,
            "script_name": "",
            "current_scene": None,
            "query_scene": None,
            "next_scene": None,
            "confidence": 0.0,
            "evidence": [],
            "days_since_script_activity": 0,
        }

    ranked_scripts = script_scores.most_common(2)
    active_script, top_score = ranked_scripts[0]
    second_score = ranked_scripts[1][1] if len(ranked_scripts) > 1 else 0.0
    if top_score <= 0:
        return {
            "active_script": None,
            "script_name": "",
            "current_scene": None,
            "query_scene": None,
            "next_scene": None,
            "confidence": 0.0,
            "evidence": [],
            "days_since_script_activity": 0,
        }

    scene_counter = scene_scores.get(active_script, Counter())
    query_scene_counter = query_scene_scores.get(active_script, Counter())
    current_scene = scene_counter.most_common(1)[0][0] if scene_counter else None
    query_scene = query_scene_counter.most_common(1)[0][0] if query_scene_counter else None

    next_scene = None
    scenes = [scene.get("name") for scene in SCRIPT_LIBRARY.get(active_script, {}).get("scenes", [])]
    scene_anchor = query_scene or current_scene
    if scene_anchor in scenes:
        index = scenes.index(scene_anchor)
        if index + 1 < len(scenes):
            next_scene = scenes[index + 1]

    last_activity = last_activity_for_script.get(active_script)
    days_since_script_activity = 0
    if last_activity is not None:
        days_since_script_activity = max(
            0,
            int((reference_now - last_activity).total_seconds() // 86400),
        )

    confidence = min(1.0, top_score / max(1.0, top_score + second_score))
    if top_score >= 4.0:
        confidence = max(confidence, 0.72)

    return {
        "active_script": active_script,
        "script_name": humanize_script_name(active_script),
        "current_scene": current_scene,
        "query_scene": query_scene,
        "next_scene": next_scene,
        "confidence": round(confidence, 3),
        "evidence": evidence.get(active_script, [])[:4],
        "days_since_script_activity": days_since_script_activity,
    }


def compute_script_relevance(
    *,
    frame_type: str | None,
    item: dict[str, Any] | None,
    script_context: dict[str, Any] | None,
) -> float:
    if not frame_type or not script_context:
        return 0.0

    active_script = script_context.get("active_script")
    if not active_script:
        return 0.0

    script_def = SCRIPT_LIBRARY.get(str(active_script), {})
    scenes = list(script_def.get("scenes", []))
    if not scenes:
        return 0.0

    desired_scene = script_context.get("query_scene") or script_context.get("current_scene")
    next_scene = script_context.get("next_scene")
    item_blob = _build_text_blob(item)
    best = 0.0

    for scene in scenes:
        scene_name = scene.get("name")
        score = 0.0

        if frame_type in scene.get("frame_types", []):
            if scene_name == desired_scene:
                score = 0.95 if script_context.get("query_scene") else 0.78
            elif scene_name == next_scene:
                score = 0.66
            else:
                score = 0.42

        for indicator in scene.get("indicators", []):
            indicator_l = str(indicator).casefold()
            if indicator_l and indicator_l in item_blob:
                if scene_name == desired_scene:
                    score = max(score, 0.88)
                elif scene_name == next_scene:
                    score = max(score, 0.62)
                else:
                    score = max(score, 0.4)

        best = max(best, score)

    confidence = float(script_context.get("confidence", 0.0) or 0.0)
    if confidence > 0:
        best *= 0.75 + min(0.25, confidence * 0.25)
    return round(min(1.0, best), 4)
