from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

try:
    from .. import config
    from . import embedder
except ImportError:
    import config
    from indexer import embedder


logger = logging.getLogger(__name__)

HAS_TESSERACT = config.HAS_TESSERACT


def _gemini_vision_active() -> bool:
    """Check at call-time so the flag can be toggled at runtime without restart."""
    return (
        bool(config.GEMINI_API_KEY)
        and bool(getattr(config, "HAS_GEMINI_SDK", False))
        and bool(getattr(config, "GEMINI_VISION_ENABLED", False))
    )


# Module-level alias used by importers (evaluated once at import time).
# For runtime checks inside this module, prefer _gemini_vision_active().
HAS_GEMINI_VISION = _gemini_vision_active()
HAS_VISUAL_MODEL = HAS_TESSERACT or HAS_GEMINI_VISION

_gemini_model = None
_gemini_error_logged = False
_pytesseract_error_logged = False
MIN_DIMENSION_FOR_OCR = 200
MIN_FILE_SIZE_FOR_OCR = 10_000
ENTROPY_THRESHOLD = 3.5
EDGE_SCORE_THRESHOLD = 8.0
OCR_CONFIG_SCREENSHOT = "--psm 3 --oem 1 -c tessedit_do_invert=0"
OCR_MIN_SIZE_BYTES = MIN_FILE_SIZE_FOR_OCR
IMAGE_MIN_OCR_BYTES = OCR_MIN_SIZE_BYTES
INITIAL_SCAN_OCR_MAX_AGE_DAYS = 730


def _log_once(flag_name: str, message: str) -> None:
    global _gemini_error_logged, _pytesseract_error_logged

    if flag_name == "gemini" and not _gemini_error_logged:
        logger.warning(message)
        _gemini_error_logged = True
    if flag_name == "pytesseract" and not _pytesseract_error_logged:
        logger.warning(message)
        _pytesseract_error_logged = True


def _get_gemini_model():
    global _gemini_model

    if not _gemini_vision_active():
        if config.GEMINI_API_KEY and not getattr(config, "HAS_GEMINI_SDK", False):
            _log_once(
                "gemini",
                "Gemini vision is disabled because google-generativeai is not installed.",
            )
        return None
    if _gemini_model is not None:
        return _gemini_model

    try:
        import google.generativeai as genai
    except ImportError:
        _log_once(
            "gemini",
            "Gemini vision is unavailable at runtime; falling back to OCR or filename.",
        )
        return None

    genai.configure(api_key=config.GEMINI_API_KEY)
    _gemini_model = genai.GenerativeModel(config.ENRICHMENT_MODEL)
    return _gemini_model


def get_visual_index() -> embedder.TextFaissIndex:
    return embedder.get_text_index()


def _entropy_reason(image_path: Path) -> tuple[bool, str]:
    with Image.open(str(image_path)) as opened_image:
        gray = opened_image.convert("L")
        small = gray.resize((64, 64), Image.LANCZOS)
        values = np.asarray(small, dtype=np.float32)

    histogram, _ = np.histogram(values, bins=64, range=(0, 256))
    total = float(histogram.sum())
    if total <= 0:
        return False, "empty_histogram"

    normalized = histogram / total
    nonzero = normalized[normalized > 0]
    entropy = float(-np.sum(nonzero * np.log2(nonzero)))
    if entropy >= ENTROPY_THRESHOLD:
        return True, f"high_entropy_{entropy:.1f}"

    edge_score = float(
        np.abs(np.diff(values, axis=1)).mean()
        + np.abs(np.diff(values, axis=0)).mean()
    )
    if edge_score >= EDGE_SCORE_THRESHOLD:
        return True, f"text_edges_{edge_score:.1f}"

    return False, f"low_entropy_{entropy:.1f}"


def should_ocr(
    image_path: Path,
    *,
    initial_scan: bool = False,
    force_ocr: bool = False,
) -> tuple[bool, str]:
    if force_ocr:
        return True, "forced"

    try:
        stat_result = Path(image_path).stat()
    except OSError:
        return False, "stat_failed"

    if stat_result.st_size < MIN_FILE_SIZE_FOR_OCR:
        return False, f"tiny_file_{max(1, stat_result.st_size // 1024)}KB"

    if initial_scan:
        age_days = (time.time() - stat_result.st_mtime) / 86400
        if age_days > INITIAL_SCAN_OCR_MAX_AGE_DAYS:
            return False, f"too_old_{age_days:.0f}d"

    try:
        with Image.open(str(image_path)) as opened_image:
            width, height = opened_image.size

        if width < MIN_DIMENSION_FOR_OCR or height < MIN_DIMENSION_FOR_OCR:
            return False, f"small_dims_{width}x{height}"

        if width >= 800 and height >= 600:
            return True, f"large_dims_{width}x{height}"

        return _entropy_reason(image_path)
    except Exception as exc:
        return True, f"inspection_failed_{type(exc).__name__}"


def _should_defer_visual_ocr(reason: str) -> bool:
    return str(reason or "").startswith("too_old_")


def _fallback_image_text(image_path: Path) -> str:
    parent = image_path.parent.name.strip() or "files"
    title = image_path.stem.strip() or image_path.name
    return f"Image file {title} in {parent}"


def _compose_visual_search_text(
    image_path: Path,
    ocr_text: str,
    *,
    text_source: str = "",
) -> tuple[str, str]:
    normalized_ocr = str(ocr_text or "").strip()
    fallback_text = _fallback_image_text(image_path)

    if not normalized_ocr:
        return fallback_text, (text_source or "filename")

    if len(normalized_ocr) >= 30 or text_source == "gemini":
        return normalized_ocr, (text_source or "ocr_success")

    combined = f"{fallback_text}\nVisible text: {normalized_ocr}"
    return combined, "ocr_partial"


def _extract_ocr_text(image_path: Path) -> str:
    if not HAS_TESSERACT:
        return ""

    try:
        import pytesseract
    except ImportError:
        _log_once(
            "pytesseract",
            "pytesseract is not installed; visual OCR will fall back to Gemini or filename.",
        )
        return ""

    try:
        if config.TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
        with Image.open(str(image_path)) as opened_image:
            image = opened_image.convert("RGB")
            t0 = time.perf_counter()
            ocr_text = pytesseract.image_to_string(
                image,
                config=OCR_CONFIG_SCREENSHOT,
            ).strip()
            ocr_elapsed = time.perf_counter() - t0
            text_len = len(ocr_text.strip())
            print(
                f"[OCR] {image_path.name[:40]:<40} | "
                f"time={ocr_elapsed:.2f}s | "
                f"chars={text_len:5d} | "
                f"{'USEFUL' if text_len > 50 else 'EMPTY'}"
            )
            return ocr_text
    except Exception as exc:
        logger.warning("OCR failed for %s: %s", image_path.name, exc)
        return ""


def _describe_with_gemini(image_path: Path) -> str:
    if not _gemini_vision_active():
        return ""

    prompt = (
        "Describe what you see in this image in 3-4 sentences. "
        "Focus on: any text visible, document type, main subject, colors/layout. "
        "Be specific and factual. Do not invent content."
    )

    try:
        model = _get_gemini_model()
        if model is None:
            return ""
        with Image.open(str(image_path)) as opened_image:
            image = opened_image.convert("RGB")
            response = model.generate_content([prompt, image])
        text = (getattr(response, "text", "") or "").strip()
        if text:
            logger.info("Used Gemini vision for %s", image_path.name)
        return text
    except Exception as exc:
        _log_once("gemini", f"Gemini vision unavailable: {exc}")
        return ""


def embed_and_index_image(
    item_id: str,
    image_path: Path,
    conn: sqlite3.Connection,
    *,
    initial_scan: bool = False,
    force_ocr: bool = False,
) -> dict[str, Any]:
    image_path = Path(image_path)
    ocr_pending = False
    audit_timings = {
        "extract": 0.0,
        "embed": 0.0,
        "db_write": 0.0,
        "ocr": 0.0,
        "gemini": 0.0,
    }

    extract_t0 = time.perf_counter()
    try:
        file_size = image_path.stat().st_size
    except OSError:
        file_size = None

    should_run_ocr, ocr_reason = should_ocr(
        image_path,
        initial_scan=initial_scan,
        force_ocr=force_ocr,
    )

    print(
        f"[OCR_DECISION] {image_path.name}: "
        f"ocr={should_run_ocr} reason={ocr_reason}"
    )

    if not should_run_ocr:
        text = _fallback_image_text(image_path)
        text_source = "filename_only"
        ocr_pending = _should_defer_visual_ocr(ocr_reason)
    elif should_run_ocr:
        ocr_t0 = time.perf_counter()
        text = _extract_ocr_text(image_path)
        audit_timings["ocr"] = time.perf_counter() - ocr_t0
        text_source = "ocr_success" if len(text.strip()) >= 30 else ""

        if len(text.strip()) < 30 and _gemini_vision_active():
            gemini_t0 = time.perf_counter()
            gemini_text = _describe_with_gemini(image_path)
            audit_timings["gemini"] = time.perf_counter() - gemini_t0
            if gemini_text:
                text = gemini_text
                text_source = "gemini"

        text, text_source = _compose_visual_search_text(
            image_path,
            text,
            text_source=text_source,
        )
    else:
        text = _fallback_image_text(image_path)
        text_source = "filename"
        ocr_pending = True
    audit_timings["extract"] = time.perf_counter() - extract_t0

    embed_t0 = time.perf_counter()
    embed_result = embedder.embed_and_index_text(
        item_id,
        text,
        conn,
        source_label=image_path.name,
        summary_text=text,
    )
    audit_timings["embed"] = time.perf_counter() - embed_t0
    faiss_id = embed_result["faiss_id"]

    now = datetime.now(timezone.utc).isoformat()
    db_write_t0 = time.perf_counter()
    conn.execute(
        """
        INSERT OR REPLACE INTO embeddings (
            item_id,
            embedding_type,
            faiss_id,
            sparse_weights,
            model_name,
            created_at
        ) VALUES (?, 'visual', ?, NULL, ?, ?)
        """,
        (item_id, faiss_id, config.EMBEDDING_MODEL, now),
    )
    conn.commit()
    audit_timings["db_write"] = time.perf_counter() - db_write_t0

    return {
        "faiss_id": faiss_id,
        "text_source": text_source,
        "text": text,
        "keywords": embed_result.get("keywords", []),
        "ocr_pending": ocr_pending,
        "audit_timings": audit_timings,
    }


def embed_query_visual(query: str) -> np.ndarray:
    return embedder.embed_text(f"search_query: {query}")
