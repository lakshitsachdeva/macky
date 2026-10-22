from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .. import config
except ImportError:
    import config


logger = logging.getLogger(__name__)

_model: Any | None = None
_genai_configured = False
_enrichment_disabled_until: datetime | None = None
_gemini_sdk_missing_logged = False


def _get_gemini_sdk() -> tuple[Any | None, type[BaseException] | None]:
    global _gemini_sdk_missing_logged

    if not config.GEMINI_API_KEY or not getattr(config, "HAS_GEMINI_SDK", False):
        if (
            config.GEMINI_API_KEY
            and not getattr(config, "HAS_GEMINI_SDK", False)
            and not _gemini_sdk_missing_logged
        ):
            logger.warning(
                "Gemini API key is set but google-generativeai is not installed; enrichment will use local heuristics."
            )
            _gemini_sdk_missing_logged = True
        return None, None

    try:
        import google.generativeai as genai
        from google.api_core.exceptions import ResourceExhausted
    except ImportError:
        if not _gemini_sdk_missing_logged:
            logger.warning(
                "google-generativeai is unavailable at runtime; enrichment will use local heuristics."
            )
            _gemini_sdk_missing_logged = True
        return None, None

    return genai, ResourceExhausted


def _get_model() -> Any:
    global _model, _genai_configured

    genai, _ = _get_gemini_sdk()
    if genai is None:
        return None

    if not _genai_configured:
        genai.configure(api_key=config.GEMINI_API_KEY)
        _genai_configured = True

    if _model is None:
        _model = genai.GenerativeModel(config.ENRICHMENT_MODEL)

    return _model


def _is_enrichment_disabled() -> bool:
    if _enrichment_disabled_until is None:
        return False
    return datetime.now(timezone.utc) < _enrichment_disabled_until


def _strip_json_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _build_prompt(filename: str, text: str) -> str:
    return (
        "Analyze this document and return ONLY valid JSON:\n"
        "{\n"
        '  "summary": "2-3 sentence description of what this document is about",\n'
        '  "doc_type": "invoice|research_paper|presentation|notes|legal|code|resume|other",\n'
        '  "importance_score": 0.7\n'
        "}\n"
        f"Filename: {filename}\n"
        f"Text (first 2000 chars): {text[:2000]}\n"
        "Return ONLY the JSON. No markdown."
    )


def _parse_enrichment_response(response_text: str) -> dict[str, Any]:
    payload = json.loads(_strip_json_fences(response_text))
    summary = str(payload.get("summary", "")).strip()
    doc_type = str(payload.get("doc_type", "other")).strip() or "other"

    try:
        importance_score = float(payload.get("importance_score", 0.5))
    except (TypeError, ValueError):
        importance_score = 0.5

    importance_score = max(0.0, min(1.0, importance_score))

    return {
        "summary": summary,
        "doc_type": doc_type,
        "importance_score": importance_score,
    }


def _generate_with_gemini(filename: str, text: str) -> dict[str, Any]:
    model = _get_model()
    if model is None:
        raise RuntimeError("Gemini API key is not configured")

    response = model.generate_content(_build_prompt(filename, text))
    response_text = getattr(response, "text", "") or ""
    if not response_text.strip():
        raise ValueError("Gemini returned an empty response")

    return _parse_enrichment_response(response_text)


def _heuristic_enrich(filename: str, text: str) -> dict[str, Any]:
    lowered_name = filename.lower()
    suffix = Path(filename).suffix.lower()

    if any(pattern in lowered_name for pattern in ("invoice", "receipt", "bill")):
        doc_type = "invoice"
    elif any(pattern in lowered_name for pattern in ("resume", "cv")):
        doc_type = "resume"
    elif suffix == ".md":
        doc_type = "notes"
    elif any(pattern in lowered_name for pattern in ("paper", "research", "study", "report")):
        doc_type = "research_paper"
    else:
        doc_type = "other"

    snippet = " ".join(text.splitlines()).strip()[:180]
    summary = f"{snippet}..." if snippet else "..."

    return {
        "summary": summary,
        "doc_type": doc_type,
        "importance_score": 0.5,
    }


async def enrich_item(item_id: str, text: str, filename: str) -> dict[str, Any]:
    del item_id

    global _enrichment_disabled_until

    _, resource_exhausted_error = _get_gemini_sdk()

    if config.GEMINI_API_KEY and resource_exhausted_error and not _is_enrichment_disabled():
        try:
            return await asyncio.to_thread(_generate_with_gemini, filename, text)
        except Exception as exc:
            if resource_exhausted_error and isinstance(exc, resource_exhausted_error):
                _enrichment_disabled_until = datetime.now(timezone.utc) + timedelta(hours=1)
                logger.warning(
                    "Gemini enrichment quota exhausted; disabling enrichment until %s",
                    _enrichment_disabled_until.isoformat(),
                )
            else:
                logger.debug("Gemini enrichment failed for %s: %s", filename, exc)

    return _heuristic_enrich(filename, text)


def format_for_db(enrichment: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": enrichment.get("summary", ""),
        "doc_type_guess": enrichment.get("doc_type", "other"),
        "importance_score": float(enrichment.get("importance_score", 0.5)),
        "enrichment_tier": 2,
    }
