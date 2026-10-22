from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import custody, database
    from .llm.local_llm import LocalLLM
except ImportError:
    import custody
    import database
    from llm.local_llm import LocalLLM


logger = logging.getLogger(__name__)

FORENSIC_DISCLAIMER = (
    "This case-scoped analysis is an investigative aid based on local records. "
    "It does not determine legal admissibility or make a legal conclusion."
)
_MODEL_STATUS_REDACT_FIELDS = {"api_key", "gemini_key", "token", "access_token", "secret"}


def _safe_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _clip(value: Any, limit: int = 1000) -> str:
    text = _safe_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        parsed = datetime.fromisoformat(cleaned)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_sort_value(value: Any) -> datetime | None:
    try:
        return parse_timestamp(str(value)) if value else None
    except ValueError:
        return None


def _file_mtime_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _json_details(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _evidence_context(row: dict[str, Any]) -> dict[str, Any]:
    source_path = str(row.get("source_path") or row.get("original_path") or "")
    return {
        "source_path": source_path,
        "source_name": str(row.get("filename") or Path(source_path).name or ""),
        "sha256": row.get("sha256"),
        "size_bytes": int(row.get("size_bytes") or 0),
        "mtime_ns": int(row.get("mtime_ns") or 0),
        "imported_at": row.get("imported_at"),
        "acquisition_status": row.get("acquisition_status"),
    }


def _item_context(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": row.get("item_id") or row.get("id"),
        "title": row.get("title") or row.get("filename"),
        "filename": row.get("filename"),
        "file_type": row.get("file_type"),
        "handler_type": row.get("handler_type"),
        "source_type": row.get("source_type"),
        "original_path": row.get("original_path"),
        "timestamps": {
            "created_at": row.get("item_created_at") or row.get("created_at"),
            "modified_at": row.get("item_modified_at") or row.get("modified_at"),
            "first_seen_at": row.get("item_first_seen_at") or row.get("first_seen_at"),
            "file_mtime": row.get("indexed_file_mtime") or row.get("file_mtime"),
            "file_mtime_at": _file_mtime_iso(
                row.get("indexed_file_mtime") or row.get("file_mtime")
            ),
        },
        "content_metadata": {
            "summary": _clip(row.get("summary"), 500),
            "preview_text": _clip(row.get("preview_text"), 500),
            "extracted_text_available": bool(_safe_text(row.get("extracted_text"))),
            "extracted_text_length": len(str(row.get("extracted_text") or "")),
        },
        "indexed_status": row.get("indexed_status"),
        "is_excluded": bool(row.get("is_excluded")) if row.get("is_excluded") is not None else None,
    }


def _timeline_timestamp(row: dict[str, Any]) -> str | None:
    return (
        str(row.get("imported_at"))
        if row.get("imported_at")
        else str(row.get("item_first_seen_at") or row.get("item_created_at") or "")
        or None
    )


def _timeline_event_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    timestamp = _timestamp_sort_value(entry.get("timestamp"))
    return (
        timestamp is None,
        timestamp or datetime.max.replace(tzinfo=timezone.utc),
        0 if entry.get("timeline_type") == "evidence" else 1,
        str(entry.get("item_id") or ""),
        str(entry.get("id") or ""),
    )


def build_forensic_timeline(
    case_id: str,
    evidence_rows: list[dict[str, Any]],
    custody_rows: list[dict[str, Any]],
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Combine only case evidence rows and case-local custody events.

    Evidence rows are the authority for item membership. A custody event with a
    non-null item_id is ignored unless that item is present in this case's
    evidence manifest. Case-level custody events (item_id is null) remain
    visible because they do not identify an out-of-case item.
    """
    start = parse_timestamp(start_at)
    end = parse_timestamp(end_at)
    if start is not None and end is not None and start > end:
        raise ValueError("start_at must be earlier than or equal to end_at")

    scoped_evidence = [
        row for row in evidence_rows if str(row.get("case_id") or case_id) == str(case_id)
    ]
    evidence_by_item = {
        str(row.get("item_id")): row
        for row in scoped_evidence
        if row.get("item_id")
    }
    entries: list[dict[str, Any]] = []

    for row in scoped_evidence:
        item_id = str(row.get("item_id") or "")
        timestamp = _timeline_timestamp(row)
        entries.append(
            {
                "id": f"evidence:{case_id}:{item_id}",
                "case_id": str(case_id),
                "timeline_type": "evidence",
                "event_type": "evidence_registered",
                "timestamp": timestamp,
                "item_id": item_id or None,
                "evidence": _evidence_context(row),
                "item": _item_context(row),
                "custody": None,
            }
        )

    scoped_custody: list[dict[str, Any]] = []
    for row in custody_rows:
        if str(row.get("case_id") or case_id) != str(case_id):
            continue
        item_id = str(row.get("item_id") or "")
        if item_id and item_id not in evidence_by_item:
            continue
        scoped_custody.append(row)
        evidence = evidence_by_item.get(item_id) if item_id else None
        entries.append(
            {
                "id": row.get("id"),
                "case_id": str(case_id),
                "timeline_type": "custody",
                "event_type": row.get("event_type"),
                "timestamp": row.get("occurred_at"),
                "item_id": item_id or None,
                "actor": row.get("actor"),
                "event_hash": row.get("event_hash"),
                "previous_event_hash": row.get("previous_event_hash"),
                "evidence": _evidence_context(evidence) if evidence else None,
                "item": _item_context(evidence) if evidence else None,
                "custody": {
                    "event_id": row.get("id"),
                    "event_type": row.get("event_type"),
                    "actor": row.get("actor"),
                    "occurred_at": row.get("occurred_at"),
                    "details": _json_details(row.get("details_json")),
                    "event_hash": row.get("event_hash"),
                    "previous_event_hash": row.get("previous_event_hash"),
                },
            }
        )

    def in_bounds(entry: dict[str, Any]) -> bool:
        timestamp = _timestamp_sort_value(entry.get("timestamp"))
        if start is not None and (timestamp is None or timestamp < start):
            return False
        if end is not None and (timestamp is None or timestamp > end):
            return False
        return True

    filtered_entries = [entry for entry in entries if in_bounds(entry)]
    filtered_entries.sort(key=_timeline_event_sort_key)
    total_entries = len(filtered_entries)
    if limit is not None:
        filtered_entries = filtered_entries[: int(limit)]

    return {
        "case_id": str(case_id),
        "entries": filtered_entries,
        "evidence_count": len(scoped_evidence),
        "custody_event_count": len(scoped_custody),
        "entry_count": total_entries,
        "truncated": limit is not None and total_entries > int(limit),
        "bounds": {
            "start_at": start.isoformat() if start is not None else None,
            "end_at": end.isoformat() if end is not None else None,
        },
    }


def _safe_model_status(status: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in dict(status or {}).items():
        lowered = str(key).casefold()
        if lowered in _MODEL_STATUS_REDACT_FIELDS or any(
            marker in lowered for marker in ("key", "token", "secret")
        ):
            continue
        safe[str(key)] = value
    return safe


def _observe_source(row: dict[str, Any]) -> dict[str, Any]:
    path_text = str(row.get("source_path") or row.get("original_path") or "").strip()
    expected = {
        "sha256": row.get("sha256"),
        "size_bytes": int(row.get("size_bytes") or 0),
        "mtime_ns": int(row.get("mtime_ns") or 0),
    }
    if not path_text:
        return {
            "status": "unreadable",
            "error": "source path is empty",
            "expected": expected,
            "observed": None,
        }
    if "://" in path_text:
        return {
            "status": "unreadable",
            "error": "source is not a local readable path",
            "expected": expected,
            "observed": None,
        }

    path = Path(path_text).expanduser()
    try:
        path.stat()
        digest = custody.hash_file_streaming(path)
        final_stat = path.stat()
    except FileNotFoundError:
        return {
            "status": "missing",
            "error": "source file is missing",
            "expected": expected,
            "observed": None,
        }
    except (OSError, PermissionError) as exc:
        return {
            "status": "unreadable",
            "error": str(exc),
            "expected": expected,
            "observed": None,
        }
    except Exception as exc:  # pragma: no cover - defensive filesystem boundary
        logger.debug("Forensic source observation failed", exc_info=True)
        return {
            "status": "unreadable",
            "error": str(exc),
            "expected": expected,
            "observed": None,
        }

    observed = {
        "sha256": digest,
        "size_bytes": int(final_stat.st_size),
        "mtime_ns": int(final_stat.st_mtime_ns),
    }
    status = "verified" if observed == expected else "changed"
    return {
        "status": status,
        "error": None,
        "expected": expected,
        "observed": observed,
    }


def _build_source_records(
    evidence_rows: list[dict[str, Any]],
    custody_rows: list[dict[str, Any]],
    *,
    case_id: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    scoped_evidence = [
        row for row in evidence_rows if str(row.get("case_id") or case_id) == str(case_id)
    ]
    scoped_evidence.sort(
        key=lambda row: (
            _timestamp_sort_value(row.get("imported_at")) is None,
            _timestamp_sort_value(row.get("imported_at"))
            or datetime.max.replace(tzinfo=timezone.utc),
            str(row.get("source_path") or ""),
            str(row.get("item_id") or ""),
        )
    )
    all_item_ids = {
        str(row.get("item_id")) for row in scoped_evidence if row.get("item_id")
    }
    selected_rows = scoped_evidence[:limit]
    selected_item_ids = {
        str(row.get("item_id")) for row in selected_rows if row.get("item_id")
    }

    scoped_events: list[dict[str, Any]] = []
    for event in custody_rows:
        if str(event.get("case_id") or case_id) != str(case_id):
            continue
        item_id = str(event.get("item_id") or "")
        if item_id and item_id not in all_item_ids:
            continue
        if not item_id or item_id in selected_item_ids:
            scoped_events.append(
                {
                    "id": event.get("id"),
                    "case_id": str(case_id),
                    "item_id": item_id or None,
                    "event_type": event.get("event_type"),
                    "actor": event.get("actor"),
                    "occurred_at": event.get("occurred_at"),
                    "details": _json_details(event.get("details_json")),
                    "previous_event_hash": event.get("previous_event_hash"),
                    "event_hash": event.get("event_hash"),
                }
            )

    events_by_item: dict[str, list[dict[str, Any]]] = {}
    case_events: list[dict[str, Any]] = []
    for event in scoped_events:
        item_id = str(event.get("item_id") or "")
        if item_id:
            events_by_item.setdefault(item_id, []).append(event)
        else:
            case_events.append(event)

    warnings: list[str] = []

    def add_warning(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    sources: list[dict[str, Any]] = []
    for index, row in enumerate(selected_rows, start=1):
        item_id = str(row.get("item_id") or "")
        source_path = str(row.get("source_path") or row.get("original_path") or "")
        source_name = str(row.get("filename") or Path(source_path).name or "")
        observation = _observe_source(row)
        acquisition_status = str(row.get("acquisition_status") or "unknown")
        if observation["status"] in {"missing", "changed", "unreadable"}:
            add_warning(
                f"Source [{index}] {source_name or item_id} is {observation['status']}: "
                f"{observation.get('error') or 'observed metadata differs from the evidence record'}."
            )
        if acquisition_status not in {"acquired", "verified"}:
            add_warning(
                f"Source [{index}] {source_name or item_id} has recorded acquisition status "
                f"{acquisition_status}."
            )
        if row.get("is_excluded"):
            add_warning(f"Source [{index}] {source_name or item_id} is marked excluded in the index.")
        if not row.get("filename") and not source_path:
            add_warning(f"Source [{index}] has no source name or path metadata.")
        if row.get("item_id") and row.get("filename") is None:
            add_warning(f"Source [{index}] item {item_id} has no indexed item metadata.")

        source = {
            "source_index": index,
            "item_id": item_id or None,
            "title": row.get("title") or source_name or item_id,
            "filename": row.get("filename") or source_name,
            "source_name": source_name,
            "source_path": source_path,
            "path": source_path,
            "sha256": row.get("sha256"),
            "acquisition_status": acquisition_status,
            "modified_at": row.get("item_modified_at") or row.get("modified_at"),
            "evidence": _evidence_context(row),
            "indexed": _item_context(row),
            "content_excerpt": _clip(
                row.get("extracted_text") or row.get("preview_text") or row.get("summary"),
                1200,
            ),
            "verification": observation,
            "custody_context": [*case_events, *events_by_item.get(item_id, [])],
        }
        sources.append(source)

    return sources, warnings, scoped_events


def _fallback_report(
    question: str,
    sources: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    if not sources:
        return {
            "summary": "No case evidence is available for this question.",
            "summary_source_indices": [],
            "findings": [],
            "limitations": [
                "The case contains no evidence rows within the requested scope.",
                "The deterministic fallback does not infer facts from records that are not present.",
            ],
        }

    findings: list[dict[str, Any]] = []
    for source in sources:
        status = source.get("verification", {}).get("status") or "unknown"
        modified_at = source.get("modified_at") or "unknown"
        statement = (
            f"{source.get('source_name') or source.get('item_id') or 'Unnamed source'} "
            f"is recorded with acquisition status {source.get('acquisition_status') or 'unknown'}, "
            f"current observation status {status}, and indexed modified timestamp {modified_at}."
        )
        excerpt = _safe_text(source.get("content_excerpt"))
        if excerpt:
            statement += f" Indexed content metadata begins: {excerpt}"
        findings.append(
            {
                "statement": statement,
                "source_indices": [int(source["source_index"])],
            }
        )

    limitations = [
        "The deterministic fallback reports recorded evidence and indexed metadata without inferring facts beyond those records.",
        f"The requested question was: {question}",
    ]
    if warnings:
        limitations.append("Review the source warnings before relying on any finding.")
    return {
        "summary": (
            f"Case-scoped review found {len(sources)} evidence source(s) for the requested question. "
            "The findings below are manifest and indexed-metadata observations."
        ),
        "summary_source_indices": [int(source["source_index"]) for source in sources],
        "findings": findings,
        "limitations": limitations,
    }


def _parse_model_json(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("model output must be a JSON object")
    if isinstance(payload.get("report"), dict):
        payload = payload["report"]
    return payload


def _validated_indices(value: Any, source_count: int, *, required: bool) -> list[int]:
    if value is None:
        if required:
            raise ValueError("missing source_indices")
        return []
    if not isinstance(value, list):
        raise ValueError("source indices must be a list")
    normalized: list[int] = []
    for raw_index in value:
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise ValueError("source indices must be integers")
        if raw_index < 1 or raw_index > source_count:
            raise ValueError("source index is outside the returned source range")
        if raw_index not in normalized:
            normalized.append(raw_index)
    if required and not normalized:
        raise ValueError("at least one source index is required")
    return normalized


def _validate_model_report(payload: dict[str, Any], source_count: int) -> dict[str, Any]:
    summary = _safe_text(payload.get("summary"))
    if not summary:
        raise ValueError("model report summary is empty")
    summary_indices = _validated_indices(
        payload.get("summary_source_indices"),
        source_count,
        required=source_count > 0,
    )

    raw_findings = payload.get("findings", [])
    if not isinstance(raw_findings, list):
        raise ValueError("model findings must be a list")
    findings: list[dict[str, Any]] = []
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict):
            raise ValueError("each finding must be an object")
        statement = _safe_text(raw_finding.get("statement") or raw_finding.get("text"))
        if not statement:
            raise ValueError("finding statement is empty")
        findings.append(
            {
                "statement": statement,
                "source_indices": _validated_indices(
                    raw_finding.get("source_indices") or raw_finding.get("citations"),
                    source_count,
                    required=source_count > 0,
                ),
            }
        )
    if len(findings) > 20:
        raise ValueError("model returned too many findings")

    raw_limitations = payload.get("limitations", [])
    if not isinstance(raw_limitations, list):
        raise ValueError("model limitations must be a list")
    limitations = [_clip(value, 500) for value in raw_limitations if _safe_text(value)]
    if len(limitations) > 20:
        limitations = limitations[:20]
    return {
        "summary": summary,
        "summary_source_indices": summary_indices,
        "findings": findings,
        "limitations": limitations,
    }


def _build_model_prompt(
    question: str,
    sources: list[dict[str, Any]],
    custody_events: list[dict[str, Any]],
) -> str:
    source_lines: list[str] = []
    for source in sources:
        source_lines.append(
            "\n".join(
                [
                    f"[SOURCE {source['source_index']}]",
                    f"item_id={source.get('item_id') or ''}",
                    f"sha256={source.get('sha256') or ''}",
                    f"acquisition_status={source.get('acquisition_status') or ''}",
                    # Do not send absolute local evidence paths to an external
                    # provider. The local report still returns the full path
                    # for the investigator, but the model only needs a stable
                    # source name plus the case-scoped hash.
                    f"source_name={source.get('source_name') or ''}",
                    f"modified_at={source.get('modified_at') or ''}",
                    f"verification={source.get('verification', {}).get('status') or ''}",
                    f"indexed_content={source.get('content_excerpt') or ''}",
                ]
            )
        )
    custody_block = json.dumps(custody_events, ensure_ascii=False, sort_keys=True)
    return (
        "You are assisting with a neutral, case-scoped forensic evidence review. "
        "Use only the supplied evidence and custody records. Do not invent facts, "
        "do not infer identity or intent, and do not state or imply legal admissibility. "
        "Return JSON only with this exact shape: "
        '{"summary": string, "summary_source_indices": [integer], '
        '"findings": [{"statement": string, "source_indices": [integer]}], '
        '"limitations": [string]}. Every summary and finding must cite one or more '
        "valid SOURCE indices.\n\n"
        f"QUESTION: {question}\n\n"
        "EVIDENCE SOURCES:\n"
        + ("\n\n".join(source_lines) if source_lines else "(none)")
        + "\n\nCASE-SCOPED CUSTODY EVENTS:\n"
        + custody_block
    )


async def build_forensic_report(
    *,
    case: dict[str, Any],
    question: str,
    evidence_rows: list[dict[str, Any]],
    custody_rows: list[dict[str, Any]],
    allow_model: bool,
    limit: int,
    llm: Any | None = None,
) -> dict[str, Any]:
    case_id = str(case.get("id") or "")
    sources, warnings, scoped_events = await asyncio.to_thread(
        _build_source_records,
        evidence_rows,
        custody_rows,
        case_id=case_id,
        limit=limit,
    )

    generation: dict[str, Any] = {
        "model_requested": bool(allow_model),
        "model_used": False,
        "provider": "heuristic",
        "fallback_used": True,
        "fallback_reason": "model_not_requested" if not allow_model else "empty_case",
        "model_status": {
            "available": False,
            "provider": "not_requested" if not allow_model else "not_used_empty_case",
            "tier": "quality",
        },
    }
    report = _fallback_report(question, sources, warnings)

    if allow_model and sources:
        model = llm or LocalLLM("quality")
        try:
            model_status = _safe_model_status(model.status())
        except Exception:
            model_status = {"available": False, "provider": "unavailable", "tier": "quality"}
        generation["model_status"] = model_status
        if bool(model_status.get("available")):
            try:
                generated = await model.generate(
                    _build_model_prompt(question, sources, scoped_events),
                    max_tokens=1400,
                    temperature=0.1,
                )
                report = _validate_model_report(
                    _parse_model_json(generated),
                    len(sources),
                )
                generation.update(
                    {
                        "model_used": True,
                        "provider": str(model_status.get("provider") or "local"),
                        "fallback_used": False,
                        "fallback_reason": None,
                    }
                )
            except ValueError:
                generation["fallback_reason"] = "invalid_model_output_or_citations"
                warnings.append(
                    "The requested model output did not pass structured citation validation; deterministic fallback used."
                )
            except Exception:
                logger.warning("Forensic report model generation failed", exc_info=True)
                generation["fallback_reason"] = "model_error"
                warnings.append(
                    "The requested model was unavailable during report generation; deterministic fallback used."
                )
        else:
            generation["fallback_reason"] = "provider_unavailable"
            warnings.append(
                "No configured local LLM provider was available; deterministic fallback used."
            )

    report_basis = {
        "case": {
            "id": case.get("id"),
            "name": case.get("name"),
            "status": case.get("status"),
        },
        "question": question,
        "report": report,
        "sources": sources,
        "custody_events": scoped_events,
        "warnings": warnings,
    }
    report_sha256 = hashlib.sha256(
        database.canonical_json(report_basis).encode("utf-8")
    ).hexdigest()

    return {
        "case_id": case_id,
        "case": {
            "id": case.get("id"),
            "name": case.get("name"),
            "status": case.get("status"),
        },
        "question": question,
        "report": report,
        "sources": sources,
        "custody_events": scoped_events,
        "warnings": warnings,
        "generation": generation,
        "provider": generation["provider"],
        "fallback": generation["fallback_used"],
        "report_sha256": report_sha256,
        "disclaimer": FORENSIC_DISCLAIMER,
        "evidence_count": len(
            [row for row in evidence_rows if str(row.get("case_id") or case_id) == case_id]
        ),
        "source_count": len(sources),
    }
