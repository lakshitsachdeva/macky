from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable

try:
    from .. import config
    from ..cognition.heuristics import (
        extract_graph_entities_from_item,
        infer_frame,
        is_person_like_entity,
    )
    from ..cognition.store import CognitiveStore
    from .store import GraphStore, get_store as get_graph_store, normalize_entity_key
except ImportError:
    import config
    from cognition.heuristics import (
        extract_graph_entities_from_item,
        infer_frame,
        is_person_like_entity,
    )
    from cognition.store import CognitiveStore
    from graph.store import GraphStore, get_store as get_graph_store, normalize_entity_key


SKIP_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "me.com",
    "outlook.com",
    "protonmail.com",
    "yahoo.com",
}
CO_OCCURRENCE_ELIGIBLE_FRAMES = {"proposal", "meeting_notes", "report", "research_notes"}
CO_OCCURRENCE_SKIPPED_FRAMES = {"resume", "email", "invoice", "contract", "pitch_deck"}
PERSON_STOPLIST = {
    "project",
    "manager",
    "team",
    "company",
    "client",
    "user",
    "author",
    "director",
    "ceo",
    "cto",
    "founder",
    "engineer",
    "developer",
    "unknown",
    "anonymous",
    "n/a",
}
FILENAME_REFERENCE_PATTERN = re.compile(
    r"\b[A-Za-z0-9][A-Za-z0-9 _.-]{1,80}\.(?:pdf|docx|doc|xlsx|xls|pptx|csv|txt|md)\b"
)
QUOTED_REFERENCE_PATTERN = re.compile(
    r"\b(?:the|our|this)\s+([A-Z][A-Za-z0-9 _-]{2,40}\s+(?:proposal|nda|deck|report|plan))\b"
)
THREAD_KEYWORD_PATTERN = re.compile(r"^thread:(.+)$", re.IGNORECASE)


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


def _choose_item_time(item: dict[str, Any]) -> str:
    for field in ("created_at", "first_seen_at", "modified_at", "last_opened_at"):
        parsed = _parse_iso(item.get(field))
        if parsed is not None:
            return parsed.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _keywords(item: dict[str, Any]) -> list[str]:
    raw = item.get("keywords")
    if isinstance(raw, list):
        return [str(entry).strip() for entry in raw if str(entry).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return [text]
    if isinstance(payload, list):
        return [str(entry).strip() for entry in payload if str(entry).strip()]
    return [text]


def _thread_id_from_item(item: dict[str, Any]) -> str | None:
    for keyword in _keywords(item):
        match = THREAD_KEYWORD_PATTERN.match(keyword)
        if match:
            return str(match.group(1)).strip()
    return None


def _message_id_from_item(item: dict[str, Any]) -> str | None:
    original_path = str(item.get("original_path") or "").strip()
    if not original_path:
        return None
    token = original_path.rstrip("/").split("/")[-1].strip()
    return token or None


def _company_from_domain(domain: str) -> str:
    root = str(domain or "").strip().casefold().split(".")[0]
    if not root:
        return ""
    return " ".join(part.capitalize() for part in root.split("-"))


def _extract_recipients(item: dict[str, Any]) -> list[str]:
    extracted_text = str(item.get("extracted_text") or "")
    lines = extracted_text.splitlines()[:10]
    headers = []
    for line in lines:
        lower = line.casefold()
        if lower.startswith("to:") or lower.startswith("cc:") or lower.startswith("bcc:"):
            headers.append(line.split(":", 1)[1].strip())
    recipients = []
    for _, address in getaddresses(headers):
        cleaned = str(address or "").strip()
        if cleaned:
            recipients.append(cleaned)
    deduped = []
    seen: set[str] = set()
    for recipient in recipients:
        normalized = recipient.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(recipient)
    return deduped


def _text_mentions(full_text: str, phrase: str) -> int:
    normalized = re.escape(" ".join(str(phrase or "").split()).strip())
    if not normalized:
        return 0
    return len(re.findall(rf"(?<!\w){normalized}(?!\w)", full_text, flags=re.IGNORECASE))


def enrich_email_facts(
    item: dict[str, Any],
    graph_store: GraphStore | None = None,
    *,
    reply_target_episode_id: str | None = None,
    thread_previous_episode_ids: list[str] | None = None,
    explicit_in_reply_to: bool = False,
) -> dict[str, int]:
    graph = graph_store or get_graph_store()
    sender_name = str(item.get("sender_name") or "").strip()
    sender_email = str(item.get("sender_email") or "").strip().casefold()
    if not sender_name and not sender_email:
        return {"sent_to_created": 0, "weak_signals": 0, "responds_to_created": 0, "skipped": 1}

    sender_label = sender_name or sender_email
    recipients = _extract_recipients(item)
    occurred_at = _choose_item_time(item)
    item_id = str(item.get("id") or "")
    sent_to_created = 0
    weak_signals = 0
    responds_to_created = 0

    sender_domain = sender_email.split("@", 1)[1].casefold() if "@" in sender_email else ""
    signature_window = str(item.get("extracted_text") or "")[:300].casefold()

    with graph.get_connection() as conn:
        sender_id = graph._upsert_entity_conn(
            conn,
            name=sender_label,
            entity_type="Person",
            aliases=[sender_email] if sender_email else [],
            attributes={"email": sender_email} if sender_email else {},
        )
        for recipient in recipients:
            recipient_entity_type = "Organisation" if "@" in recipient and recipient.split("@", 1)[1].casefold() not in SKIP_DOMAINS else "Person"
            recipient_id = graph._upsert_entity_conn(
                conn,
                name=recipient,
                entity_type=recipient_entity_type,
                aliases=[recipient] if "@" in recipient else [],
            )
            if sender_id != recipient_id:
                graph._add_temporal_fact_conn(
                    conn,
                    subject_id=sender_id,
                    predicate="SENT_TO",
                    object_id=recipient_id,
                    valid_from=occurred_at,
                    episode_id=item_id,
                    confidence=1.0,
                )
                sent_to_created += 1

    if sender_domain and sender_domain not in SKIP_DOMAINS:
        organisation_name = _company_from_domain(sender_domain)
        if organisation_name:
            existing_org = graph.get_entity_by_name(organisation_name)
            confidence = 0.8 if existing_org is not None else 0.4
            if organisation_name.casefold() in signature_window:
                confidence = max(confidence, 0.6)
            count, should_promote = graph.record_weak_signal(
                sender_label,
                organisation_name,
                "REPRESENTS_ORG",
                item_id,
                confidence,
            )
            weak_signals += 1
            if should_promote and count >= 2:
                graph.run_promotions()

    target_episode_id = reply_target_episode_id
    if target_episode_id is None and str(item.get("title") or "").strip().casefold().startswith("re:"):
        history = [episode_id for episode_id in (thread_previous_episode_ids or []) if episode_id and episode_id != item_id]
        if history:
            target_episode_id = history[-1]
    if target_episode_id:
        graph.link_episode_to_episode(
            item_id,
            "RESPONDS_TO",
            target_episode_id,
            confidence=1.0 if explicit_in_reply_to else 0.7,
        )
        responds_to_created += 1

    graph.expire_old_signals()
    return {
        "sent_to_created": sent_to_created,
        "weak_signals": weak_signals,
        "responds_to_created": responds_to_created,
        "skipped": 0,
    }


def backfill_email_facts(
    *,
    memory_db_path: str | Path | None = None,
    graph_store: GraphStore | None = None,
    limit: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    graph = graph_store or get_graph_store()
    memory_path = Path(memory_db_path or config.DB_PATH).expanduser()
    conn = sqlite3.connect(str(memory_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM items
            WHERE source_type = 'gmail'
              AND COALESCE(is_excluded, 0) = 0
            ORDER BY COALESCE(created_at, first_seen_at, modified_at) ASC
            LIMIT ?
            """,
            (int(limit) if limit is not None else 10_000_000,),
        ).fetchall()
    finally:
        conn.close()

    processed = 0
    sent_to_created = 0
    weak_signal_count = 0
    responds_to_created = 0
    skipped = 0
    thread_map: dict[str, list[str]] = {}

    for row in rows:
        item = dict(row)
        thread_id = _thread_id_from_item(item)
        previous = list(thread_map.get(thread_id or "", []))
        result = enrich_email_facts(
            item,
            graph,
            thread_previous_episode_ids=previous,
        )
        processed += 1
        sent_to_created += int(result["sent_to_created"])
        weak_signal_count += int(result["weak_signals"])
        responds_to_created += int(result.get("responds_to_created", 0))
        skipped += int(result["skipped"])
        if thread_id:
            thread_map.setdefault(thread_id, []).append(str(item.get("id") or ""))
        if progress_callback is not None and (processed == 1 or processed % 100 == 0 or processed == len(rows)):
            progress_callback(
                {
                    "stage": "email_backfill",
                    "processed": processed,
                    "total": len(rows),
                    "sent_to_created": sent_to_created,
                    "weak_signals": weak_signal_count,
                    "responds_to_created": responds_to_created,
                    "skipped": skipped,
                }
            )
    return {
        "processed": processed,
        "sent_to_created": sent_to_created,
        "weak_signals": weak_signal_count,
        "responds_to_created": responds_to_created,
        "skipped": skipped,
    }


def extract_document_co_occurrences(
    item: dict[str, Any],
    graph_store: GraphStore | None = None,
    *,
    cognitive_store: CognitiveStore | None = None,
) -> dict[str, int]:
    del cognitive_store
    graph = graph_store or get_graph_store()
    extracted_text = str(item.get("extracted_text") or "")
    if len(extracted_text.strip()) < 200:
        return {"weak_signals": 0, "references": 0, "skipped": 1}

    frame_type, _, _ = infer_frame(item)
    if frame_type in CO_OCCURRENCE_SKIPPED_FRAMES or frame_type not in CO_OCCURRENCE_ELIGIBLE_FRAMES:
        return {"weak_signals": 0, "references": 0, "skipped": 1}

    people = [
        entity
        for entity in extract_graph_entities_from_item(item, limit=20)
        if str(entity.get("entity_type") or "") == "Person"
    ]
    qualified_people: list[str] = []
    for entity in people:
        name = " ".join(str(entity.get("name") or "").split()).strip()
        if len(name) < 2 or name.casefold() in PERSON_STOPLIST:
            continue
        if not is_person_like_entity(
            name,
            aliases=list(entity.get("aliases") or []),
            attributes=dict(entity.get("attributes") or {}),
        ):
            continue
        if _text_mentions(extracted_text, name) < 2:
            continue
        qualified_people.append(name)
    qualified_people = list(dict.fromkeys(qualified_people))
    if len(qualified_people) < 2:
        return {"weak_signals": 0, "references": 0, "skipped": 1}

    relation = "CO_OCCURS_WITH"
    if frame_type in {"proposal", "research_notes"}:
        relation = "CO_AUTHORED"
    elif frame_type == "meeting_notes":
        relation = "CO_ATTENDED"

    weak_signals = 0
    item_id = str(item.get("id") or "")
    for index, left in enumerate(qualified_people):
        for right in qualified_people[index + 1:]:
            count, should_promote = graph.record_weak_signal(left, right, relation, item_id, 0.4)
            weak_signals += 1
            if should_promote and count >= 3:
                graph.run_promotions()

    references = 0
    current_label = str(item.get("title") or item.get("filename") or item_id)
    for match in FILENAME_REFERENCE_PATTERN.findall(extracted_text):
        graph.record_weak_signal(current_label, str(match).strip(), "REFERENCES", item_id, 0.7)
        references += 1
    for match in QUOTED_REFERENCE_PATTERN.findall(extracted_text):
        graph.record_weak_signal(current_label, str(match).strip(), "REFERENCES", item_id, 0.4)
        references += 1

    graph.run_promotions()
    graph.expire_old_signals()
    return {"weak_signals": weak_signals, "references": references, "skipped": 0}


def backfill_document_co_occurrences(
    *,
    memory_db_path: str | Path | None = None,
    graph_store: GraphStore | None = None,
    limit: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    graph = graph_store or get_graph_store()
    memory_path = Path(memory_db_path or config.DB_PATH).expanduser()
    conn = sqlite3.connect(str(memory_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM items
            WHERE source_type != 'gmail'
              AND source_type != 'gmail_attachment'
              AND COALESCE(is_excluded, 0) = 0
            ORDER BY COALESCE(created_at, first_seen_at, modified_at) ASC
            LIMIT ?
            """,
            (int(limit) if limit is not None else 10_000_000,),
        ).fetchall()
    finally:
        conn.close()

    processed = 0
    weak_signals = 0
    references = 0
    skipped = 0
    for row in rows:
        result = extract_document_co_occurrences(dict(row), graph)
        processed += 1
        weak_signals += int(result["weak_signals"])
        references += int(result["references"])
        skipped += int(result["skipped"])
        if progress_callback is not None and (processed == 1 or processed % 100 == 0 or processed == len(rows)):
            progress_callback(
                {
                    "stage": "document_backfill",
                    "processed": processed,
                    "total": len(rows),
                    "weak_signals": weak_signals,
                    "references": references,
                    "skipped": skipped,
                }
            )
    return {
        "processed": processed,
        "weak_signals": weak_signals,
        "references": references,
        "skipped": skipped,
    }


def enrich_item_graph_context(
    item: dict[str, Any],
    *,
    graph_store: GraphStore | None = None,
) -> dict[str, Any]:
    source_type = str(item.get("source_type") or "").casefold()
    if source_type == "gmail":
        return enrich_email_facts(item, graph_store)
    if source_type in {"gmail_attachment"}:
        return {"weak_signals": 0, "references": 0, "skipped": 1}
    return extract_document_co_occurrences(item, graph_store)
