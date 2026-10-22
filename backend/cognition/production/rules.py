from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import re
import sqlite3
from string import Formatter
from typing import Any, Callable

try:
    from ... import config
    from ..heuristics import extract_entities_from_item, extract_query_entities
    from ..scripts.detector import detect_script_context
    from ..spreading import SpreadingActivationEngine
    from ..store import CognitiveStore, get_store
except ImportError:
    import config
    from cognition.heuristics import extract_entities_from_item, extract_query_entities
    from cognition.scripts.detector import detect_script_context
    from cognition.spreading import SpreadingActivationEngine
    from cognition.store import CognitiveStore, get_store


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _best_item_timestamp(item: dict[str, Any] | None) -> datetime | None:
    if not item:
        return None
    for field in ("last_opened_at", "modified_at", "created_at", "first_seen_at"):
        parsed = _normalize_datetime(item.get(field))
        if parsed is not None:
            return parsed
    return None


def _days_since(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds() // 86400))


def _hours_since(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return max(0.0, (now - value).total_seconds() / 3600.0)


def _safe_format(template: str, values: dict[str, Any]) -> str:
    formatter = Formatter()
    parts: list[str] = []
    for literal_text, field_name, format_spec, conversion in formatter.parse(template):
        del conversion
        parts.append(literal_text)
        if field_name is None:
            continue
        value = values.get(field_name, "")
        if format_spec:
            try:
                parts.append(format(value, format_spec))
            except Exception:
                parts.append(str(value))
        else:
            parts.append(str(value))
    return "".join(parts)


def _dedupe_key_for_item(item: dict[str, Any]) -> str:
    path_value = str(item.get("original_path") or "").strip().casefold()
    if path_value:
        return f"path:{path_value}"

    title_value = str(item.get("title") or "").strip().casefold()
    filename_value = str(item.get("filename") or "").strip().casefold()
    combined = " ".join(part for part in (title_value, filename_value) if part)
    normalized = re.sub(r"[^a-z0-9]+", " ", combined).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return f"title:{normalized}"


def _rule_unanswered_email_surface(ctx: dict[str, Any]) -> bool:
    return (
        ctx.get("current_frame") in {"email", "proposal"}
        and len(ctx.get("unanswered_emails", [])) > 0
    )


def _rule_stalled_script(ctx: dict[str, Any]) -> bool:
    return (
        ctx.get("active_script") is not None
        and int(ctx.get("days_since_script_activity", 0) or 0) > 14
    )


def _rule_prior_work_surface(ctx: dict[str, Any]) -> bool:
    return (
        float(ctx.get("file_age_hours", 999) or 999) < 24
        and int(ctx.get("spreading_hit_count", 0) or 0) > 3
    )


def _rule_invoice_follow_up(ctx: dict[str, Any]) -> bool:
    return (
        ctx.get("current_frame") == "invoice"
        and len(ctx.get("unpaid_invoices", [])) > 0
    )


def _rule_script_next_step(ctx: dict[str, Any]) -> bool:
    return (
        ctx.get("active_script") is not None
        and ctx.get("current_scene") is not None
        and ctx.get("next_scene") is not None
    )


def _rule_temporal_anniversary(ctx: dict[str, Any]) -> bool:
    return len(ctx.get("anniversary_hits", [])) > 0


def _rule_incomplete_frame_surface(ctx: dict[str, Any]) -> bool:
    current_slots = ctx.get("current_slots", {}) or {}
    return (
        ctx.get("current_frame") == "invoice"
        and str(current_slots.get("status") or "").lower() == "unpaid"
        and int(ctx.get("invoice_age_days", 0) or 0) > 30
    )


PRODUCTION_RULES = [
    {
        "id": "unanswered_email_surface",
        "priority": 9,
        "cooldown_hours": 48,
        "condition": _rule_unanswered_email_surface,
        "action": {
            "type": "surface_items",
            "message": "{sender} emailed you {days_ago} days ago — you haven't replied",
        },
        "surface_key": "unanswered_emails",
    },
    {
        "id": "stalled_script",
        "priority": 8,
        "cooldown_hours": 24,
        "condition": _rule_stalled_script,
        "action": {
            "type": "script_warning",
            "message": "Your {script_name} has been stalled for {days} days — was this abandoned?",
        },
    },
    {
        "id": "prior_work_surface",
        "priority": 7,
        "cooldown_hours": 12,
        "condition": _rule_prior_work_surface,
        "action": {
            "type": "proactive_briefing",
            "message": "Before you continue — here's what you already have on this topic:",
        },
        "surface_key": "related_items",
    },
    {
        "id": "invoice_follow_up",
        "priority": 8,
        "cooldown_hours": 72,
        "condition": _rule_invoice_follow_up,
        "action": {
            "type": "surface_items",
            "message": "You have {count} invoices that may still be outstanding",
        },
        "surface_key": "unpaid_invoices",
    },
    {
        "id": "script_next_step",
        "priority": 6,
        "cooldown_hours": 6,
        "condition": _rule_script_next_step,
        "action": {
            "type": "script_suggestion",
            "message": "In {script_name}: you're at '{current_scene}' — next is '{next_scene}'",
        },
    },
    {
        "id": "temporal_anniversary",
        "priority": 3,
        "cooldown_hours": 168,
        "condition": _rule_temporal_anniversary,
        "action": {
            "type": "temporal_surface",
            "message": "A year ago today, you were working on:",
        },
        "surface_key": "anniversary_hits",
    },
    {
        "id": "incomplete_frame_surface",
        "priority": 10,
        "cooldown_hours": 12,
        "condition": _rule_incomplete_frame_surface,
        "action": {
            "type": "urgent_surface",
            "message": "This invoice is {days} days overdue",
        },
        "surface_key": "current_item_surface",
    },
]


class ProductionEngine:
    def __init__(
        self,
        cognitive_store: CognitiveStore | None = None,
        *,
        memory_db_path: str | Path | None = None,
        current_time_fn: Callable[[], datetime] | None = None,
    ):
        self.store = cognitive_store or get_store()
        self.memory_db_path = Path(memory_db_path or config.DB_PATH).expanduser()
        self.current_time_fn = current_time_fn or _utc_now

    def evaluate(self, context: dict[str, Any], *, log_fires: bool = True) -> list[dict[str, Any]]:
        now = self.current_time_fn()
        fired: list[dict[str, Any]] = []

        for rule in PRODUCTION_RULES:
            last_fired = self.store.get_last_production_fire(rule["id"])
            if last_fired is not None:
                elapsed_seconds = (now - last_fired).total_seconds()
                if elapsed_seconds < float(rule["cooldown_hours"]) * 3600:
                    continue

            try:
                if not rule["condition"](context):
                    continue
            except Exception:
                continue

            surfaced_items = list(context.get(rule.get("surface_key", ""), []) or [])
            action_payload = dict(rule["action"])
            action_payload["message"] = _safe_format(str(action_payload.get("message") or ""), context)

            fired_rule = {
                "rule_id": rule["id"],
                "priority": int(rule["priority"]),
                "cooldown_hours": float(rule["cooldown_hours"]),
                "action": action_payload,
                "surfaced_items": surfaced_items,
            }
            fired.append(fired_rule)

            if log_fires:
                self.store.log_production_fire(
                    rule["id"],
                    fired_at=now,
                    conditions=self._summarize_conditions(context),
                    surfaced_items=surfaced_items,
                )

        return sorted(fired, key=lambda item: item["priority"], reverse=True)[:3]

    def build_context(
        self,
        *,
        current_item_id: str | None = None,
        current_query: str | None = None,
    ) -> dict[str, Any]:
        now = self.current_time_fn()
        current_item = self._get_memory_item(current_item_id) if current_item_id else None
        current_frame = self.store.get_frame(current_item_id) if current_item_id else None
        current_slots = dict(current_frame.get("slots") or {}) if current_frame else {}
        current_frame_type = current_frame.get("frame_type") if current_frame else None

        file_age_hours = _hours_since(_best_item_timestamp(current_item), now)
        unanswered_emails = self._find_unanswered_emails(now=now)
        unpaid_invoices = self._find_unpaid_invoices(now=now)
        related_items = self._find_related_items(
            current_item=current_item,
            current_item_id=current_item_id,
            current_query=current_query,
        )
        anniversary_hits = self._find_anniversary_hits(now=now)
        script_context = self._detect_script_context(
            now=now,
            current_item=current_item,
            current_frame_type=current_frame_type,
            current_query=current_query,
        )

        invoice_age_days = None
        if current_frame_type == "invoice":
            invoice_age_days = _days_since(_best_item_timestamp(current_item), now)

        primary_email = unanswered_emails[0] if unanswered_emails else {}
        current_item_surface = []
        if current_item and current_item_id:
            current_item_surface = [self._decorate_item_summary(current_item_id, current_item, current_frame)]

        context: dict[str, Any] = {
            "current_item_id": current_item_id,
            "current_query": current_query or "",
            "current_item": current_item,
            "current_frame": current_frame_type,
            "current_slots": current_slots,
            "file_age_hours": round(file_age_hours, 2) if file_age_hours is not None else 999.0,
            "unanswered_emails": unanswered_emails,
            "unpaid_invoices": unpaid_invoices,
            "related_items": related_items,
            "spreading_hit_count": len(related_items),
            "anniversary_hits": anniversary_hits,
            "invoice_age_days": invoice_age_days or 0,
            "count": len(unpaid_invoices),
            "current_item_surface": current_item_surface,
            **script_context,
        }

        if primary_email:
            context["sender"] = primary_email.get("sender") or primary_email.get("title") or "Someone"
            context["days_ago"] = primary_email.get("days_ago", 0)

        if script_context.get("script_name"):
            context["script_name"] = script_context["script_name"]
            context["days"] = script_context.get("days_since_script_activity", 0)

        if current_frame_type == "invoice" and invoice_age_days is not None:
            context["days"] = invoice_age_days

        return context

    def _memory_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.memory_db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _get_memory_item(self, item_id: str | None) -> dict[str, Any] | None:
        if not item_id:
            return None
        with self._memory_connection() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row is not None else None

    def _get_memory_items(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._memory_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM items WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        return {str(row["id"]): dict(row) for row in rows}

    def _find_related_items(
        self,
        *,
        current_item: dict[str, Any] | None,
        current_item_id: str | None,
        current_query: str | None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        seed_entities: list[str] = []
        if current_query:
            seed_entities = extract_query_entities(current_query)
        elif current_item:
            seed_entities = [
                entity_id
                for entity_id, _ in extract_entities_from_item(current_item, limit=8)
            ]

        if not seed_entities:
            return []

        spread_engine = SpreadingActivationEngine(self.store)
        spread_scores = spread_engine.spread({entity_id: 1.0 for entity_id in seed_entities})
        ranked = [
            (node_id, score)
            for node_id, score in sorted(spread_scores.items(), key=lambda item: item[1], reverse=True)
            if node_id.startswith("item:")
        ]

        summaries: list[dict[str, Any]] = []
        candidate_item_ids = [
            node_id.split("item:", 1)[1]
            for node_id, _ in ranked
            if node_id.split("item:", 1)[1] != current_item_id
        ][: limit * 2]
        memory_items = self._get_memory_items(candidate_item_ids)

        for node_id, score in ranked:
            item_id = node_id.split("item:", 1)[1]
            if item_id == current_item_id:
                continue
            item = memory_items.get(item_id)
            if not item:
                continue
            frame = self.store.get_frame(item_id)
            summaries.append(
                {
                    **self._decorate_item_summary(item_id, item, frame),
                    "score": round(float(score), 3),
                }
            )
            if len(summaries) >= limit:
                break
        return summaries

    def _find_unanswered_emails(self, *, now: datetime, limit: int = 5) -> list[dict[str, Any]]:
        email_frames = self.store.list_frame_instances(frame_type="email", limit=100)
        item_ids = [str(frame["item_id"]) for frame in email_frames]
        memory_items = self._get_memory_items(item_ids)

        emails: list[dict[str, Any]] = []
        for frame in email_frames:
            slots = dict(frame.get("slots") or {})
            requires_response = str(slots.get("requires_response") or "unknown").lower()
            if requires_response != "yes":
                continue

            item_id = str(frame["item_id"])
            item = memory_items.get(item_id, {})
            timestamp = _best_item_timestamp(item) or _normalize_datetime(slots.get("date"))
            days_ago = _days_since(timestamp, now) or 0
            emails.append(
                {
                    **self._decorate_item_summary(item_id, item, frame),
                    "sender": slots.get("sender") or item.get("sender_name") or item.get("sender_email") or "Someone",
                    "days_ago": days_ago,
                }
            )

        emails.sort(key=lambda item: (item.get("days_ago", 0), item.get("title", "")), reverse=True)
        return emails[:limit]

    def _find_unpaid_invoices(self, *, now: datetime, limit: int = 5) -> list[dict[str, Any]]:
        invoice_frames = self.store.list_frame_instances(frame_type="invoice", limit=100)
        item_ids = [str(frame["item_id"]) for frame in invoice_frames]
        memory_items = self._get_memory_items(item_ids)

        invoices: list[dict[str, Any]] = []
        for frame in invoice_frames:
            slots = dict(frame.get("slots") or {})
            status = str(slots.get("status") or "unpaid").lower()
            if status not in {"unpaid", "overdue"}:
                continue

            item_id = str(frame["item_id"])
            item = memory_items.get(item_id, {})
            base_date = _normalize_datetime(slots.get("due_date")) or _best_item_timestamp(item)
            days_overdue = _days_since(base_date, now) or 0
            invoices.append(
                {
                    **self._decorate_item_summary(item_id, item, frame),
                    "status": status,
                    "amount": slots.get("amount"),
                    "days_overdue": days_overdue,
                }
            )

        invoices.sort(key=lambda item: (item.get("days_overdue", 0), item.get("title", "")), reverse=True)
        return invoices[:limit]

    def _find_anniversary_hits(self, *, now: datetime, limit: int = 3) -> list[dict[str, Any]]:
        target_day = (now - timedelta(days=365)).date()
        start = datetime.combine(target_day, time.min, tzinfo=timezone.utc)
        end = datetime.combine(target_day, time.max, tzinfo=timezone.utc)
        with self._memory_connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM items
                WHERE is_excluded = 0
                  AND (
                    (created_at IS NOT NULL AND created_at >= ? AND created_at <= ?)
                    OR (first_seen_at IS NOT NULL AND first_seen_at >= ? AND first_seen_at <= ?)
                    OR (modified_at IS NOT NULL AND modified_at >= ? AND modified_at <= ?)
                  )
                ORDER BY COALESCE(modified_at, created_at, first_seen_at) DESC
                LIMIT ?
                """,
                (
                    start.isoformat(),
                    end.isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                    limit * 6,
                ),
            ).fetchall()

        hits: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for row in rows:
            item = dict(row)
            dedupe_key = _dedupe_key_for_item(item)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            item_id = str(item["id"])
            frame = self.store.get_frame(item_id)
            hits.append(self._decorate_item_summary(item_id, item, frame))
            if len(hits) >= limit:
                break
        return hits

    def _detect_script_context(
        self,
        *,
        now: datetime,
        current_item: dict[str, Any] | None,
        current_frame_type: str | None,
        current_query: str | None,
    ) -> dict[str, Any]:
        if not current_item and not str(current_query or "").strip():
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

        script_context = detect_script_context(
            self.store,
            current_frame_type=current_frame_type,
            current_query=current_query,
            current_item=current_item,
            now=now,
        )
        if script_context.get("active_script"):
            self.store.upsert_script_state(
                script_context["active_script"],
                script_context.get("current_scene") or "unknown",
                last_activity=(
                    now - timedelta(days=int(script_context.get("days_since_script_activity", 0) or 0))
                ),
                entities_involved=extract_query_entities(current_query or "")[:6],
            )
        return script_context

    def _decorate_item_summary(
        self,
        item_id: str,
        item: dict[str, Any] | None,
        frame: dict[str, Any] | None,
    ) -> dict[str, Any]:
        item = item or {}
        frame = frame or {}
        return {
            "item_id": item_id,
            "title": item.get("title") or item.get("filename") or item_id,
            "frame_type": frame.get("frame_type"),
            "original_path": item.get("original_path"),
            "source_type": item.get("source_type"),
        }

    def _summarize_conditions(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_item_id": context.get("current_item_id"),
            "current_frame": context.get("current_frame"),
            "active_script": context.get("active_script"),
            "current_scene": context.get("current_scene"),
            "spreading_hit_count": int(context.get("spreading_hit_count", 0) or 0),
            "unanswered_emails": len(context.get("unanswered_emails", [])),
            "unpaid_invoices": len(context.get("unpaid_invoices", [])),
        }
