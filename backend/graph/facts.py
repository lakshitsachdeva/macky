from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_iso(value: str | None) -> datetime | None:
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


def choose_anchor_time(item: dict[str, Any], now: datetime) -> datetime:
    candidates = [
        parse_iso(item.get("first_seen_at")),
        parse_iso(item.get("created_at")),
        parse_iso(item.get("modified_at")),
        parse_iso(item.get("last_opened_at")),
    ]
    available = [candidate for candidate in candidates if candidate is not None]
    return min(available) if available else now


def guess_fact_entity_type(name: str) -> str:
    lowered = str(name or "").casefold()
    if "@" in lowered:
        return "Person"
    if any(
        marker in lowered
        for marker in (
            " inc",
            " llc",
            " ltd",
            " corp",
            " company",
            " university",
            " institute",
            " labs",
            " studio",
            " capital",
            " ventures",
            " technologies",
            " tech",
        )
    ):
        return "Organisation"
    if lowered in {
        "fundraising",
        "draft",
        "submitted",
        "accepted",
        "rejected",
        "unpaid",
        "paid",
        "overdue",
        "partial",
    }:
        return "Concept"
    if len(str(name).split()) >= 2 and all(part[:1].isupper() for part in str(name).split() if part):
        return "Person"
    return "Concept"


def extract_temporal_fact_specs(
    item: dict[str, Any],
    *,
    frame_type: str,
    slots: dict[str, Any],
    anchor_time: datetime,
) -> list[dict[str, Any]]:
    item_id = str(item.get("id") or "")
    if not item_id:
        return []

    facts: list[dict[str, Any]] = []

    def add_fact(
        subject: str | None,
        subject_type: str,
        predicate: str,
        object_name: str | None,
        object_type: str,
        confidence: float,
    ) -> None:
        subject_clean = " ".join(str(subject or "").split()).strip()
        object_clean = " ".join(str(object_name or "").split()).strip()
        if not subject_clean or not object_clean or subject_clean == object_clean:
            return
        facts.append(
            {
                "subject": subject_clean,
                "subject_type": subject_type,
                "predicate": predicate,
                "object": object_clean,
                "object_type": object_type,
                "valid_from": anchor_time,
                "episode_id": item_id,
                "confidence": confidence,
            }
        )

    if frame_type == "email":
        sender = slots.get("sender") or item.get("sender_name") or item.get("sender_email")
        recipients = list(slots.get("recipients") or [])
        for recipient in recipients[:6]:
            add_fact(
                sender,
                "Person",
                "SENT_TO",
                str(recipient),
                guess_fact_entity_type(str(recipient)),
                0.82,
            )

    elif frame_type == "meeting_notes":
        attendees = [str(attendee) for attendee in (slots.get("attendees") or []) if str(attendee).strip()]
        for index, attendee in enumerate(attendees[:6]):
            for colleague in attendees[index + 1:6]:
                add_fact(attendee, "Person", "COLLABORATES_WITH", colleague, "Person", 0.68)

    elif frame_type == "proposal":
        project_name = slots.get("project_name") or item.get("title") or item.get("filename")
        recipient = slots.get("recipient")
        status = slots.get("status")
        add_fact(
            recipient,
            guess_fact_entity_type(str(recipient or "")),
            "RELATES_TO",
            project_name,
            "Project",
            0.72,
        )
        add_fact(
            project_name,
            "Project",
            "HAS_STATUS",
            status,
            "Concept",
            0.84,
        )

    elif frame_type == "invoice":
        vendor = slots.get("vendor") or item.get("sender_name")
        client = slots.get("client")
        status = slots.get("status")
        invoice_number = str(slots.get("invoice_number") or "").strip()
        invoice_name = f"Invoice {invoice_number}" if invoice_number else (item.get("title") or item.get("filename"))
        add_fact(
            vendor,
            guess_fact_entity_type(str(vendor or "")),
            "RELATES_TO",
            client,
            guess_fact_entity_type(str(client or "")),
            0.74,
        )
        add_fact(
            invoice_name,
            "Document",
            "HAS_STATUS",
            status,
            "Concept",
            0.88,
        )

    elif frame_type == "pitch_deck":
        company_name = slots.get("company_name") or item.get("title") or item.get("filename")
        add_fact(
            company_name,
            guess_fact_entity_type(str(company_name or "")),
            "HAS_STATUS",
            "fundraising",
            "Concept",
            0.9,
        )

    elif frame_type == "contract":
        parties = [str(party) for party in (slots.get("parties") or []) if str(party).strip()]
        for index, party in enumerate(parties[:6]):
            for other_party in parties[index + 1:6]:
                add_fact(
                    party,
                    guess_fact_entity_type(party),
                    "COLLABORATES_WITH",
                    other_party,
                    guess_fact_entity_type(other_party),
                    0.78,
                )

    return facts
