from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from .query_parser import ParsedQuery
    from .search import SearchResult, parse_iso
except ImportError:
    from retrieval.query_parser import ParsedQuery
    from retrieval.search import SearchResult, parse_iso


TOPIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "by",
    "for",
    "from",
    "got",
    "how",
    "i",
    "in",
    "is",
    "it",
    "last",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "week",
    "with",
    "you",
    "your",
}


def _topic_phrase(parsed: ParsedQuery) -> str:
    semantic_tokens = [
        token
        for token in (parsed.semantic_text or "").split()
        if len(token) > 1 and token.casefold() not in TOPIC_STOPWORDS
    ]
    if semantic_tokens:
        return " ".join(semantic_tokens[:2])
    return "your query"


def _relative_time(iso_str: str | None) -> str | None:
    if iso_str is None:
        return None

    parsed = parse_iso(iso_str)
    if parsed is None:
        return None

    delta = datetime.now(timezone.utc) - parsed
    days = max(0, delta.days)

    if delta.total_seconds() < 24 * 3600:
        return "today"
    if delta.total_seconds() < 48 * 3600:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "last week"
    if days < 60:
        weeks = max(1, round(days / 7))
        return f"{weeks} weeks ago"
    if days < 365:
        return parsed.strftime("%B %d").replace(" 0", " ")

    months = max(1, round(days / 30))
    return f"{months} months ago"


def _source_path(item: dict[str, Any]) -> str:
    source_type = item.get("source_type", "local")
    return {
        "email": "Email -> Downloads",
        "download": "Browser -> Downloads",
        "screenshot": "Screenshots folder",
        "browser": "Browser History",
        "local": "Local Files",
    }.get(source_type, "Local Files")


def explain_match(result: SearchResult, parsed: ParsedQuery) -> dict[str, Any]:
    reasons: list[str] = []
    item = result.item

    if result.dense_score > 0.65:
        topic = _topic_phrase(parsed)
        reasons.append(f"Semantically matches '{topic}'")

    if result.sparse_score > 0.4:
        reasons.append("Contains keywords from your search")

    if result.visual_score > 0.65:
        reasons.append("Visual content matches your description")

    if result.fts_score > 0.5:
        reasons.append("Exact text match found")

    if result.filename_score > 0.4:
        reasons.append("Filename matches your search")

    if result.content_score > 0.4:
        reasons.append("Document text matches your search")

    if result.path_score > 0.4:
        reasons.append("Folder path matches your search")

    if result.time_score > 0.8 and parsed.time_window:
        reasons.append(f"Matches '{parsed.time_window.description}'")

    if result.metadata_score > 0.8:
        if parsed.file_type and item.get("file_type") == parsed.file_type:
            reasons.append(f"Is a {parsed.file_type.upper()} file")
        if parsed.source_type and item.get("source_type") == parsed.source_type:
            reasons.append(f"From {parsed.source_type}")
        if parsed.person_hints and item.get("sender_name"):
            reasons.append(f"Sent by {item['sender_name']}")

    if result.behavior_score > 0.4:
        open_count = item.get("open_count", 0) or 0
        if open_count >= 3:
            reasons.append(f"Opened {open_count} times")
        elif (item.get("copy_event_count", 0) or 0) >= 1:
            reasons.append("You copied text from this")

    reasons = reasons[:4]

    if result.final_score >= 0.72:
        confidence = "high"
    elif result.final_score >= 0.48:
        confidence = "medium"
    else:
        confidence = "low"

    memory_trail = {
        "first_seen": _relative_time(item.get("first_seen_at")),
        "last_seen": _relative_time(item.get("last_opened_at")),
        "open_count": item.get("open_count", 0),
        "source": _source_path(item),
        "sender": item.get("sender_name"),
    }

    return {
        "reasons": reasons,
        "confidence": confidence,
        "memory_trail": memory_trail,
    }
