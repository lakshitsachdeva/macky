from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .. import config, database
    from ..llm.extraction import apply_item_knowledge, extract_item_knowledge
    from . import embedder, enricher, extractor, visual_embedder
except ImportError:
    import config
    import database
    from llm.extraction import apply_item_knowledge, extract_item_knowledge
    from indexer import embedder, enricher, extractor, visual_embedder


logger = logging.getLogger(__name__)
index_events: asyncio.Queue = asyncio.Queue()
FAST_SKIP_FILE_SIZE_BYTES = 50 * 1024 * 1024
CHUNKING_TEXT_LENGTH = 500_000
MAX_STORED_TEXT_CHARS = 250_000
_semaphore = asyncio.Semaphore(4)
_model_loaded = False
_model_load_lock = asyncio.Lock()
_active_index_jobs = 0
_active_index_jobs_lock = asyncio.Lock()
_deferred_visual_queue: asyncio.Queue = asyncio.Queue()
_deferred_visual_worker: asyncio.Task | None = None
_deferred_visual_enqueued: set[tuple[str, str]] = set()
_DEFERRED_VISUAL_IDLE_WAIT_SECONDS = 8
MACOS_DATALESS_FLAG = 0x40000000


def _cleanup_partial_index(
    conn: Any,
    item_id: str | None,
    *,
    delete_item: bool = True,
) -> None:
    if conn is None or not item_id:
        return

    try:
        conn.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
        if delete_item:
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
    except Exception:
        logger.debug("Failed to clean up partial index state for %s", item_id)


def _item_matches_path(item: dict[str, Any] | None, resolved_path: str) -> bool:
    return bool(item) and str(item.get("original_path") or "") == resolved_path


def _retire_replaced_path_item(conn: Any, item_id: str | None) -> None:
    if conn is None or not item_id:
        return

    conn.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
    conn.execute(
        "UPDATE items SET is_excluded = 1 WHERE id = ?",
        (item_id,),
    )


def _file_type_for_suffix(suffix: str) -> str:
    if suffix in config.SUPPORTED_IMAGE_EXTENSIONS:
        return "image"
    return config.SUPPORTED_TEXT_EXTENSIONS.get(suffix, "unknown")


def _has_extracted_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _merge_keywords(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            keyword = str(value or "").strip()
            if not keyword:
                continue
            normalized = keyword.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(keyword)
    return merged


def _text_for_storage(summary_text: str, full_text: str) -> str:
    normalized_full = str(full_text or "").strip()
    if len(normalized_full) <= MAX_STORED_TEXT_CHARS:
        return normalized_full

    normalized_summary = str(summary_text or "").strip()
    remaining = max(0, MAX_STORED_TEXT_CHARS - len(normalized_summary) - 2)
    if normalized_summary and remaining > 0:
        return f"{normalized_summary}\n\n{normalized_full[:remaining]}".strip()
    return normalized_full[:MAX_STORED_TEXT_CHARS].strip()


async def ensure_model_loaded() -> None:
    global _model_loaded

    if _model_loaded:
        return

    async with _model_load_lock:
        if _model_loaded:
            return
        await embedder.initialize_model_pool()
        _model_loaded = True


def _get_pdf_internal_creation_date(path: Path) -> datetime | None:
    try:
        import fitz
    except ImportError:
        return None

    document = None
    try:
        document = fitz.open(str(path))
        raw_value = (document.metadata or {}).get("creationDate")
        if not raw_value:
            return None

        date_str = str(raw_value)
        if date_str.startswith("D:"):
            date_str = date_str[2:]
        date_str = date_str[:14]
        if len(date_str) != 14:
            return None

        parsed = datetime.strptime(date_str, "%Y%m%d%H%M%S")
        return parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    finally:
        if document is not None:
            document.close()

def _get_real_timestamps(path: Path) -> dict[str, Any]:
    file_path = Path(path)
    stat_result = file_path.stat()

    modified_at = datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)

    try:
        created_at = datetime.fromtimestamp(stat_result.st_birthtime, tz=timezone.utc)
    except AttributeError:
        created_at = datetime.fromtimestamp(stat_result.st_ctime, tz=timezone.utc)

    first_seen_at = min(created_at, modified_at)
    if file_path.suffix.lower() == ".pdf":
        pdf_date = _get_pdf_internal_creation_date(file_path)
        if pdf_date is not None and pdf_date < first_seen_at:
            created_at = pdf_date
            first_seen_at = pdf_date

    return {
        "created_at": created_at.isoformat(),
        "modified_at": modified_at.isoformat(),
        "first_seen_at": first_seen_at.isoformat(),
        "file_mtime": float(stat_result.st_mtime),
        "file_size_bytes": stat_result.st_size,
    }


def get_file_timestamps(path: Path) -> dict[str, Any]:
    return _get_real_timestamps(path)


def hash_file_streaming(path: Path, chunk_size: int = 65536) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            sha.update(chunk)
    return sha.hexdigest()


def _visual_metadata_hash(path: Path, *, last_seen: float, file_size: int) -> str:
    normalized_path = str(Path(path).expanduser().resolve())
    payload = f"visual-metadata:{normalized_path}:{file_size}:{last_seen:.6f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _should_use_visual_metadata_hash(
    path: Path,
    *,
    file_size: int | None,
    force: bool,
) -> bool:
    if force or file_size is None:
        return False
    return (
        path.suffix.lower() in config.SUPPORTED_IMAGE_EXTENSIONS
        and int(file_size) < int(visual_embedder.OCR_MIN_SIZE_BYTES)
    )


def _format_timing_parts(timings: dict[str, float]) -> str:
    ordered_keys = ["extract", "hash", "db_lookup", "embed", "db_write", "ocr", "gemini"]
    parts: list[str] = []
    seen: set[str] = set()

    for key in ordered_keys:
        if key in timings and key != "total":
            parts.append(f"{key}: {timings[key]:.3f}s")
            seen.add(key)

    for key, value in timings.items():
        if key == "total" or key in seen:
            continue
        parts.append(f"{key}: {value:.3f}s")

    return "  ".join(parts)


def _print_timing_line(
    filename: str,
    timings: dict[str, float],
    *,
    status: str | None = None,
    extra: str | None = None,
) -> None:
    label = filename if status is None else f"{status} {filename}"
    parts = _format_timing_parts(timings)
    suffix = f" | {extra}" if extra else ""
    if parts:
        print(
            f"[TIMING] {label[:40]:<40} | {parts} | "
            f"total: {timings.get('total', 0.0):.3f}s{suffix}"
        )
        return
    print(f"[TIMING] {label[:40]:<40} | total: {timings.get('total', 0.0):.3f}s{suffix}")


def _is_database_lock_error(error_text: str) -> bool:
    normalized = str(error_text or "").casefold()
    return (
        "database is locked" in normalized
        or "database schema is locked" in normalized
    )


def _is_dataless_path(path: str | Path) -> bool:
    try:
        flags = getattr(Path(path).expanduser().resolve().stat(), "st_flags", 0)
    except OSError:
        return False
    return bool(flags & MACOS_DATALESS_FLAG)


def _mark_dataless_file(
    path: str | Path,
    *,
    conn: Any | None = None,
    item_id: str | None = None,
    last_seen: float | None = None,
    file_size: int | None = None,
    attempted_at: float | None = None,
) -> None:
    owns_conn = conn is None
    active_conn = conn or database.get_connection()
    try:
        database.mark_watched_file_dataless(
            active_conn,
            path,
            item_id=item_id,
            last_seen=last_seen,
            file_size=file_size,
            attempted_at=attempted_at,
        )
    finally:
        if owns_conn:
            active_conn.close()


async def _track_index_job_started() -> None:
    global _active_index_jobs
    async with _active_index_jobs_lock:
        _active_index_jobs += 1


async def _track_index_job_finished() -> None:
    global _active_index_jobs
    async with _active_index_jobs_lock:
        _active_index_jobs = max(0, _active_index_jobs - 1)


async def _wait_for_index_idle() -> None:
    while True:
        async with _active_index_jobs_lock:
            if _active_index_jobs == 0:
                return
        await asyncio.sleep(1)


def _index_file_sync(
    file_path: Path,
    force: bool = False,
    initial_scan: bool = False,
) -> dict[str, Any]:
    conn = None
    item_id: str | None = None
    delete_item_on_error = True
    resolved_path = ""
    last_seen: float | None = None
    file_size: int | None = None
    attempted_at: float | None = None
    timings: dict[str, float] = {}
    started_at = time.perf_counter()

    try:
        file_path = Path(file_path)
        resolved_path = str(file_path.resolve())

        suffix = file_path.suffix.lower()
        if suffix not in config.SUPPORTED_EXTENSIONS:
            return {"status": "skipped", "reason": "unsupported_extension"}

        if not file_path.exists() or file_path.is_dir():
            return {"status": "skipped", "reason": "not_found"}

        # Skip files inside user-excluded directories
        resolved = file_path.resolve()
        for excl in config.EXCLUDED_DIRS:
            try:
                resolved.relative_to(excl)
                return {"status": "skipped", "reason": "excluded_dir"}
            except ValueError:
                pass

        stat = file_path.stat()
        last_seen = float(stat.st_mtime)
        file_size = int(stat.st_size)
        if getattr(stat, "st_flags", 0) & MACOS_DATALESS_FLAG:
            attempted_at = time.time()
            _mark_dataless_file(
                resolved_path,
                last_seen=last_seen,
                file_size=file_size,
                attempted_at=attempted_at,
            )
            return {"status": "error", "error": database.DATALLESS_FILE_ERROR}

        if stat.st_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
            return {"status": "skipped", "reason": "too_large"}

        age_days = (datetime.now().timestamp() - stat.st_mtime) / 86400
        if (
            age_days > config.MAX_FILE_AGE_DAYS
            and not (initial_scan and suffix in config.SUPPORTED_IMAGE_EXTENSIONS)
            and not force
        ):
            return {"status": "skipped", "reason": "too_old"}

        timestamps = _get_real_timestamps(file_path)
        last_seen = float(timestamps["file_mtime"])
        file_size = int(timestamps["file_size_bytes"])

        conn = database.get_connection()
        attempted_at = database.record_watched_file_attempt(
            conn,
            resolved_path,
            last_seen=last_seen,
            file_size=file_size,
        )

        hash_t0 = time.perf_counter()
        use_metadata_hash = _should_use_visual_metadata_hash(
            file_path,
            file_size=file_size,
            force=force,
        )
        if use_metadata_hash:
            content_hash = _visual_metadata_hash(
                file_path,
                last_seen=last_seen,
                file_size=file_size or 0,
            )
        else:
            try:
                content_hash = hash_file_streaming(file_path)
            except (TimeoutError, OSError):
                if _should_use_visual_metadata_hash(
                    file_path,
                    file_size=file_size,
                    force=force,
                ):
                    logger.warning(
                        "Falling back to metadata hash for small image %s",
                        file_path,
                    )
                    content_hash = _visual_metadata_hash(
                        file_path,
                        last_seen=last_seen,
                        file_size=file_size or 0,
                    )
                else:
                    raise
        timings["hash"] = time.perf_counter() - hash_t0

        lookup_t0 = time.perf_counter()
        existing_by_hash = database.get_item_by_hash(conn, content_hash)
        existing_by_path = database.get_item_by_path(conn, resolved_path)
        timings["db_lookup"] = time.perf_counter() - lookup_t0

        existing: dict[str, Any] | None = None
        if existing_by_hash is not None:
            if _item_matches_path(existing_by_hash, resolved_path) or (
                existing_by_path is not None
                and str(existing_by_path.get("id")) == str(existing_by_hash.get("id"))
            ):
                if not force:
                    db_write_t0 = time.perf_counter()
                    database.update_item(
                        conn,
                        str(existing_by_hash["id"]),
                        {
                            "created_at": timestamps["created_at"],
                            "modified_at": timestamps["modified_at"],
                            "first_seen_at": timestamps["first_seen_at"],
                            "file_mtime": timestamps["file_mtime"],
                            "file_size_bytes": timestamps["file_size_bytes"],
                        },
                    )
                    database.upsert_watched_file(
                        conn,
                        resolved_path,
                        item_id=str(existing_by_hash["id"]),
                        last_seen=last_seen,
                        file_size=file_size,
                        status="indexed",
                        last_attempted=attempted_at,
                    )
                    timings["db_write"] = time.perf_counter() - db_write_t0
                    timings["total"] = time.perf_counter() - started_at
                    _print_timing_line(
                        file_path.name,
                        timings,
                        status="SKIP",
                        extra="already_indexed",
                    )
                    conn.close()
                    return {"status": "already_indexed", "item_id": existing_by_hash["id"]}

                existing = existing_by_hash
            else:
                db_write_t0 = time.perf_counter()
                if existing_by_path is not None:
                    _retire_replaced_path_item(conn, str(existing_by_path["id"]))
                database.upsert_watched_file(
                    conn,
                    resolved_path,
                    item_id=str(existing_by_hash["id"]),
                    last_seen=last_seen,
                    file_size=file_size,
                    status="duplicate",
                    last_attempted=attempted_at,
                )
                conn.commit()
                timings["db_write"] = time.perf_counter() - db_write_t0
                timings["total"] = time.perf_counter() - started_at
                _print_timing_line(
                    file_path.name,
                    timings,
                    status="DUPLICATE",
                )
                conn.close()
                return {
                    "status": "duplicate",
                    "item_id": existing_by_hash["id"],
                    "title": file_path.name,
                    "filename": file_path.name,
                }

        if existing is None and existing_by_path is not None:
            existing = existing_by_path

        if existing is not None:
            item_id = str(existing["id"])
            delete_item_on_error = False
        else:
            item_id = str(uuid.uuid4())

        handler_type = (
            "visual" if suffix in config.SUPPORTED_IMAGE_EXTENSIONS else "text"
        )
        item: dict[str, Any] = {
            "id": item_id,
            "content_hash": content_hash,
            "filename": file_path.name,
            "file_type": _file_type_for_suffix(suffix),
            "handler_type": handler_type,
            "source_type": "local",
            "original_path": str(file_path.resolve()),
            "created_at": timestamps["created_at"],
            "modified_at": timestamps["modified_at"],
            "first_seen_at": timestamps["first_seen_at"],
            "file_mtime": timestamps["file_mtime"],
            "file_size_bytes": timestamps["file_size_bytes"],
            "enrichment_tier": 0,
            "ocr_pending": 0,
        }

        extracted: extractor.ExtractedContent | None = None

        if handler_type == "text":
            extract_t0 = time.perf_counter()
            extracted = extractor.extract_structured(file_path)
            timings["extract"] = time.perf_counter() - extract_t0
            if extracted.error:
                logger.warning(
                    "Extraction issue for %s: %s",
                    file_path.name,
                    extracted.error,
                )

            item["title"] = extracted.title or file_path.stem
            item["extracted_text"] = _text_for_storage(
                extracted.summary_text,
                extracted.full_text,
            )
            item["preview_text"] = (extracted.first_page or extracted.summary_text)[:200]
            item["summary"] = extracted.summary_text

            heading_keywords = [
                heading.lower()
                for heading in extracted.headings[:10]
                if str(heading or "").strip()
            ]
            embed_text = extracted.summary_text or extracted.full_text or item["title"]

            use_chunking = len(extracted.full_text) > CHUNKING_TEXT_LENGTH
            embed_t0 = time.perf_counter()
            emb_result = embedder.embed_and_index_text(
                item_id,
                embed_text,
                conn,
                chunked=use_chunking,
                extra_text=extracted.full_text if use_chunking else None,
                source_label=file_path.name,
                summary_text=extracted.summary_text,
            )
            timings["embed"] = time.perf_counter() - embed_t0
            if use_chunking:
                logger.info(
                    "Chunked %s into %s pieces (indexed %s immediately)",
                    file_path.name,
                    emb_result.get("chunk_count", 0),
                    emb_result.get("indexed_chunk_count", 0),
                )
            merged_keywords = _merge_keywords(emb_result["keywords"], heading_keywords)
            item["keywords"] = json.dumps(merged_keywords[:30])
            item["enrichment_tier"] = 1 if _has_extracted_text(extracted.full_text or extracted.summary_text) else 0

        if handler_type == "visual":
            item["title"] = file_path.stem
            visual_t0 = time.perf_counter()
            visual_result = visual_embedder.embed_and_index_image(
                item_id,
                file_path,
                conn,
                initial_scan=initial_scan,
            )
            visual_elapsed = time.perf_counter() - visual_t0
            visual_timings = {
                key: float(value)
                for key, value in dict(visual_result.get("audit_timings") or {}).items()
            }
            timings["extract"] = visual_timings.get("extract", 0.0)
            timings["embed"] = visual_timings.get("embed", visual_elapsed)
            if visual_timings.get("ocr", 0.0) > 0:
                timings["ocr"] = visual_timings["ocr"]
            if visual_timings.get("gemini", 0.0) > 0:
                timings["gemini"] = visual_timings["gemini"]
            visual_text = str(visual_result.get("text") or "").strip()
            text_source = str(visual_result.get("text_source") or "").strip()
            item["extracted_text"] = visual_text
            item["preview_text"] = visual_text[:500] if visual_text else f"[Image: {file_path.name}]"
            item["ocr_pending"] = 1 if visual_result.get("ocr_pending") else 0

            keywords = visual_result.get("keywords") or []
            if keywords:
                item["keywords"] = json.dumps(keywords)

            if visual_result.get("faiss_id") is not None and text_source in {"ocr_success", "gemini"}:
                item["enrichment_tier"] = 1
            else:
                item["enrichment_tier"] = 0
                if text_source in {"filename", "filename_only", "ocr_empty"}:
                    logging.warning(
                        "Visual extraction fell back to filename for %s",
                        file_path.name,
                    )
                else:
                    logging.warning(
                        "Visual embedding unavailable for %s, indexed by filename only",
                        file_path.name,
                    )

        db_write_extra = 0.0
        if handler_type == "visual":
            visual_timings = dict(visual_result.get("audit_timings") or {})
            db_write_extra = float(visual_timings.get("db_write", 0.0))

        db_write_t0 = time.perf_counter()
        if existing is not None:
            update_fields = dict(item)
            update_fields.pop("id", None)
            database.update_item(conn, item_id, update_fields)
        else:
            database.insert_item(conn, item)
        database.upsert_watched_file(
            conn,
            resolved_path,
            item_id=item_id,
            last_seen=last_seen,
            file_size=file_size,
            status="indexed",
            last_attempted=attempted_at,
        )
        conn.commit()
        timings["db_write"] = db_write_extra + (time.perf_counter() - db_write_t0)
        conn.close()
        conn = None
        timings["total"] = time.perf_counter() - started_at
        _print_timing_line(file_path.name, timings)

        return {
            "status": "indexed",
            "item_id": item_id,
            "title": item.get("title"),
            "file_type": item["file_type"],
            "handler_type": handler_type,
            "extracted_text": str(item.get("extracted_text") or "").strip(),
            "enrichment_tier": item["enrichment_tier"],
            "ocr_pending": item["ocr_pending"],
            "filename": file_path.name,
        }
    except Exception as exc:
        logger.exception("Indexing failed for %s", file_path)
        error_message = f"{type(exc).__name__}: {str(exc)}"
        timings["total"] = time.perf_counter() - started_at
        _print_timing_line(
            file_path.name,
            timings,
            status="ERROR",
            extra=error_message[:120],
        )
        _cleanup_partial_index(conn, item_id, delete_item=delete_item_on_error)
        if conn is not None and resolved_path:
            try:
                if _is_database_lock_error(error_message):
                    database.upsert_watched_file(
                        conn,
                        resolved_path,
                        item_id=item_id if not delete_item_on_error else None,
                        last_seen=last_seen,
                        file_size=file_size,
                        status="pending",
                        last_attempted=attempted_at or time.time(),
                    )
                else:
                    if _is_dataless_path(resolved_path):
                        _mark_dataless_file(
                            resolved_path,
                            conn=conn,
                            item_id=item_id if not delete_item_on_error else None,
                            last_seen=last_seen,
                            file_size=file_size,
                            attempted_at=attempted_at,
                        )
                        attempt_count = 0
                        final_status = "error"
                    else:
                        attempt_count, final_status = database.record_watched_file_failure(
                            conn,
                            resolved_path,
                            error_message,
                            item_id=item_id if not delete_item_on_error else None,
                            last_seen=last_seen,
                            file_size=file_size,
                            attempted_at=attempted_at,
                        )
                    print(
                        f"[PIPELINE] Error indexing {file_path.name} "
                        f"(attempt {attempt_count}): {error_message[:200]} "
                        f"-> status={final_status}"
                    )
            except Exception:
                logger.debug("Failed to record watched file error state for %s", resolved_path)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return {"status": "error", "error": str(exc)}


async def _index_text_file_async(
    file_path: Path,
    force: bool = False,
    initial_scan: bool = False,
) -> dict[str, Any]:
    conn = None
    item_id: str | None = None
    delete_item_on_error = True
    resolved_path = ""
    last_seen: float | None = None
    file_size: int | None = None
    attempted_at: float | None = None
    timings: dict[str, float] = {}
    started_at = time.perf_counter()

    try:
        file_path = Path(file_path)
        resolved_path = str(file_path.resolve())

        suffix = file_path.suffix.lower()
        if suffix not in config.SUPPORTED_EXTENSIONS:
            return {"status": "skipped", "reason": "unsupported_extension"}
        if suffix in config.SUPPORTED_IMAGE_EXTENSIONS:
            return {"status": "skipped", "reason": "visual_path"}
        if not file_path.exists() or file_path.is_dir():
            return {"status": "skipped", "reason": "not_found"}

        stat = file_path.stat()
        last_seen = float(stat.st_mtime)
        file_size = int(stat.st_size)
        if getattr(stat, "st_flags", 0) & MACOS_DATALESS_FLAG:
            attempted_at = time.time()
            _mark_dataless_file(
                resolved_path,
                last_seen=last_seen,
                file_size=file_size,
                attempted_at=attempted_at,
            )
            return {"status": "error", "error": database.DATALLESS_FILE_ERROR}

        if stat.st_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
            return {"status": "skipped", "reason": "too_large"}

        age_days = (datetime.now().timestamp() - stat.st_mtime) / 86400
        if age_days > config.MAX_FILE_AGE_DAYS and not force:
            return {"status": "skipped", "reason": "too_old"}

        timestamps = _get_real_timestamps(file_path)
        last_seen = float(timestamps["file_mtime"])
        file_size = int(timestamps["file_size_bytes"])

        conn = database.get_connection()
        attempted_at = database.record_watched_file_attempt(
            conn,
            resolved_path,
            last_seen=last_seen,
            file_size=file_size,
        )

        hash_t0 = time.perf_counter()
        content_hash = await asyncio.to_thread(hash_file_streaming, file_path)
        timings["hash"] = time.perf_counter() - hash_t0

        lookup_t0 = time.perf_counter()
        existing_by_hash = database.get_item_by_hash(conn, content_hash)
        existing_by_path = database.get_item_by_path(conn, resolved_path)
        timings["db_lookup"] = time.perf_counter() - lookup_t0

        existing: dict[str, Any] | None = None
        if existing_by_hash is not None:
            if _item_matches_path(existing_by_hash, resolved_path) or (
                existing_by_path is not None
                and str(existing_by_path.get("id")) == str(existing_by_hash.get("id"))
            ):
                if not force:
                    db_write_t0 = time.perf_counter()
                    database.update_item(
                        conn,
                        str(existing_by_hash["id"]),
                        {
                            "created_at": timestamps["created_at"],
                            "modified_at": timestamps["modified_at"],
                            "first_seen_at": timestamps["first_seen_at"],
                            "file_mtime": timestamps["file_mtime"],
                            "file_size_bytes": timestamps["file_size_bytes"],
                        },
                    )
                    database.upsert_watched_file(
                        conn,
                        resolved_path,
                        item_id=str(existing_by_hash["id"]),
                        last_seen=last_seen,
                        file_size=file_size,
                        status="indexed",
                        last_attempted=attempted_at,
                    )
                    timings["db_write"] = time.perf_counter() - db_write_t0
                    timings["total"] = time.perf_counter() - started_at
                    _print_timing_line(
                        file_path.name,
                        timings,
                        status="SKIP",
                        extra="already_indexed",
                    )
                    conn.close()
                    return {"status": "already_indexed", "item_id": existing_by_hash["id"]}

                existing = existing_by_hash
            else:
                db_write_t0 = time.perf_counter()
                if existing_by_path is not None:
                    _retire_replaced_path_item(conn, str(existing_by_path["id"]))
                database.upsert_watched_file(
                    conn,
                    resolved_path,
                    item_id=str(existing_by_hash["id"]),
                    last_seen=last_seen,
                    file_size=file_size,
                    status="duplicate",
                    last_attempted=attempted_at,
                )
                conn.commit()
                timings["db_write"] = time.perf_counter() - db_write_t0
                timings["total"] = time.perf_counter() - started_at
                _print_timing_line(
                    file_path.name,
                    timings,
                    status="DUPLICATE",
                )
                conn.close()
                return {
                    "status": "duplicate",
                    "item_id": existing_by_hash["id"],
                    "title": file_path.name,
                    "filename": file_path.name,
                }

        if existing is None and existing_by_path is not None:
            existing = existing_by_path

        if existing is not None:
            item_id = str(existing["id"])
            delete_item_on_error = False
        else:
            item_id = str(uuid.uuid4())

        item: dict[str, Any] = {
            "id": item_id,
            "content_hash": content_hash,
            "filename": file_path.name,
            "file_type": _file_type_for_suffix(suffix),
            "handler_type": "text",
            "source_type": "local",
            "original_path": resolved_path,
            "created_at": timestamps["created_at"],
            "modified_at": timestamps["modified_at"],
            "first_seen_at": timestamps["first_seen_at"],
            "file_mtime": timestamps["file_mtime"],
            "file_size_bytes": timestamps["file_size_bytes"],
            "enrichment_tier": 0,
            "ocr_pending": 0,
        }

        extract_t0 = time.perf_counter()
        extracted = await asyncio.to_thread(extractor.extract_structured, file_path)
        timings["extract"] = time.perf_counter() - extract_t0
        if extracted.error:
            logger.warning(
                "Extraction issue for %s: %s",
                file_path.name,
                extracted.error,
            )

        item["title"] = extracted.title or file_path.stem
        item["extracted_text"] = _text_for_storage(
            extracted.summary_text,
            extracted.full_text,
        )
        item["preview_text"] = (extracted.first_page or extracted.summary_text)[:200]
        item["summary"] = extracted.summary_text

        heading_keywords = [
            heading.lower()
            for heading in extracted.headings[:10]
            if str(heading or "").strip()
        ]
        embed_text = extracted.summary_text or extracted.full_text or item["title"]
        use_chunking = len(str(extracted.full_text or "")) > CHUNKING_TEXT_LENGTH

        embed_t0 = time.perf_counter()
        emb_result = await embedder.embed_and_index_text_async(
            item_id,
            embed_text,
            conn,
            chunked=use_chunking,
            extra_text=extracted.full_text if use_chunking else None,
            source_label=file_path.name,
            summary_text=extracted.summary_text,
        )
        timings["embed"] = time.perf_counter() - embed_t0
        if use_chunking:
            logger.info(
                "Chunked %s into %s pieces (indexed %s immediately)",
                file_path.name,
                emb_result.get("chunk_count", 0),
                emb_result.get("indexed_chunk_count", 0),
            )

        merged_keywords = _merge_keywords(emb_result["keywords"], heading_keywords)
        item["keywords"] = json.dumps(merged_keywords[:30])
        item["enrichment_tier"] = 1 if _has_extracted_text(
            extracted.full_text or extracted.summary_text
        ) else 0

        db_write_t0 = time.perf_counter()
        if existing is not None:
            update_fields = dict(item)
            update_fields.pop("id", None)
            database.update_item(conn, item_id, update_fields)
        else:
            database.insert_item(conn, item)
        database.upsert_watched_file(
            conn,
            resolved_path,
            item_id=item_id,
            last_seen=last_seen,
            file_size=file_size,
            status="indexed",
            last_attempted=attempted_at,
        )
        conn.commit()
        timings["db_write"] = time.perf_counter() - db_write_t0
        conn.close()
        conn = None
        timings["total"] = time.perf_counter() - started_at
        _print_timing_line(file_path.name, timings)

        return {
            "status": "indexed",
            "item_id": item_id,
            "title": item.get("title"),
            "file_type": item["file_type"],
            "handler_type": "text",
            "extracted_text": str(item.get("extracted_text") or "").strip(),
            "enrichment_tier": item["enrichment_tier"],
            "ocr_pending": item["ocr_pending"],
            "filename": file_path.name,
        }
    except Exception as exc:
        logger.exception("Indexing failed for %s", file_path)
        error_message = f"{type(exc).__name__}: {str(exc)}"
        timings["total"] = time.perf_counter() - started_at
        _print_timing_line(
            file_path.name,
            timings,
            status="ERROR",
            extra=error_message[:120],
        )
        _cleanup_partial_index(conn, item_id, delete_item=delete_item_on_error)
        if conn is not None and resolved_path:
            try:
                if _is_database_lock_error(error_message):
                    database.upsert_watched_file(
                        conn,
                        resolved_path,
                        item_id=item_id if not delete_item_on_error else None,
                        last_seen=last_seen,
                        file_size=file_size,
                        status="pending",
                        last_attempted=attempted_at or time.time(),
                    )
                else:
                    if _is_dataless_path(resolved_path):
                        _mark_dataless_file(
                            resolved_path,
                            conn=conn,
                            item_id=item_id if not delete_item_on_error else None,
                            last_seen=last_seen,
                            file_size=file_size,
                            attempted_at=attempted_at,
                        )
                        attempt_count = 0
                        final_status = "error"
                    else:
                        attempt_count, final_status = database.record_watched_file_failure(
                            conn,
                            resolved_path,
                            error_message,
                            item_id=item_id if not delete_item_on_error else None,
                            last_seen=last_seen,
                            file_size=file_size,
                            attempted_at=attempted_at,
                        )
                    print(
                        f"[PIPELINE] Error indexing {file_path.name} "
                        f"(attempt {attempt_count}): {error_message[:200]} "
                        f"-> status={final_status}"
                    )
            except Exception:
                logger.debug(
                    "Failed to record watched file error state for %s",
                    resolved_path,
                )
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return {"status": "error", "error": str(exc)}


def _apply_deferred_visual_ocr_sync(item_id: str, file_path: Path) -> dict[str, Any]:
    conn = None

    try:
        path = Path(file_path)
        if not path.exists() or path.is_dir():
            return {"status": "skipped", "reason": "not_found"}

        conn = database.get_connection()
        existing = database.get_item_by_id(conn, item_id)
        if existing is None:
            return {"status": "skipped", "reason": "missing_item"}

        conn.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
        visual_result = visual_embedder.embed_and_index_image(
            item_id,
            path,
            conn,
            initial_scan=False,
            force_ocr=True,
        )

        keywords = visual_result.get("keywords") or []
        text = str(visual_result.get("text") or "").strip()
        text_source = str(visual_result.get("text_source") or "").strip()
        update_fields: dict[str, Any] = {
            "extracted_text": text,
            "preview_text": text[:500] if text else f"[Image: {path.name}]",
            "enrichment_tier": 1 if text_source in {"ocr_success", "gemini"} else 0,
            "ocr_pending": 0,
        }
        if keywords:
            update_fields["keywords"] = json.dumps(keywords)

        database.update_item(conn, item_id, update_fields)
        conn.commit()
        conn.close()
        conn = None
        return {"status": "updated", "item_id": item_id, "text_source": text_source}
    except Exception as exc:
        logger.exception("Deferred visual OCR failed for %s", file_path)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return {"status": "error", "error": str(exc)}


async def _ensure_deferred_visual_worker() -> None:
    global _deferred_visual_worker

    if _deferred_visual_worker is not None and not _deferred_visual_worker.done():
        return

    _deferred_visual_worker = asyncio.create_task(_deferred_visual_worker_loop())


async def _schedule_deferred_visual_ocr(item_id: str, file_path: Path) -> None:
    key = (str(item_id), str(Path(file_path).resolve()))
    if key in _deferred_visual_enqueued:
        return

    _deferred_visual_enqueued.add(key)
    await _ensure_deferred_visual_worker()
    await _deferred_visual_queue.put(key)


async def bootstrap_deferred_visual_ocr(limit: int | None = None) -> int:
    conn = database.get_connection()
    try:
        rows = database.get_pending_ocr_items(conn, limit=limit)
    finally:
        conn.close()

    scheduled = 0
    for row in rows:
        original_path = str(row.get("original_path") or "").strip()
        item_id = str(row.get("id") or "").strip()
        if not original_path or not item_id:
            continue
        path = Path(original_path)
        if not path.exists():
            continue
        await _schedule_deferred_visual_ocr(item_id, path)
        scheduled += 1
    return scheduled


async def _deferred_visual_worker_loop() -> None:
    while True:
        item_id, path_str = await _deferred_visual_queue.get()
        key = (item_id, path_str)
        try:
            await _wait_for_index_idle()
            await asyncio.sleep(_DEFERRED_VISUAL_IDLE_WAIT_SECONDS)
            await _wait_for_index_idle()
            result = await asyncio.to_thread(
                _apply_deferred_visual_ocr_sync,
                item_id,
                Path(path_str),
            )
            if result.get("status") == "updated":
                await index_events.put({"type": "item_enriched", "item_id": item_id})
        except Exception:
            logger.exception("Deferred visual worker crashed for %s", path_str)
        finally:
            _deferred_visual_enqueued.discard(key)
            _deferred_visual_queue.task_done()


async def index_file(
    file_path: Path,
    force: bool = False,
    initial_scan: bool = False,
) -> dict[str, Any]:
    path = Path(file_path)
    try:
        if not path.exists() or path.is_dir():
            return {"status": "skipped", "reason": "not_found"}
        if not force and path.stat().st_size > FAST_SKIP_FILE_SIZE_BYTES:
            logger.debug("Skipping large file: %s", path.name)
            return {"status": "skipped", "reason": "too_large_fast"}
    except FileNotFoundError:
        return {"status": "skipped", "reason": "not_found"}

    suffix = path.suffix.lower()
    result: dict[str, Any] = {"status": "error", "error": "indexing_not_started"}
    for attempt in range(3):
        if suffix in config.SUPPORTED_IMAGE_EXTENSIONS:
            result = await asyncio.to_thread(_index_file_sync, path, force, initial_scan)
        else:
            result = await _index_text_file_async(path, force, initial_scan)
        error_text = str(result.get("error") or "").casefold()
        if result.get("status") != "error":
            break
        if "database is locked" not in error_text and "database schema is locked" not in error_text:
            break
        await asyncio.sleep(0.5 * (attempt + 1))

    if result.get("status") == "duplicate":
        await index_events.put(
            {
                "type": "file_duplicate",
                "item_id": str(result["item_id"]),
                "title": result.get("title") or str(result.get("filename") or path.name),
                "filename": str(result.get("filename") or path.name),
            }
        )
        return {
            "status": "duplicate",
            "item_id": str(result["item_id"]),
            "title": result.get("title"),
        }

    if result.get("status") != "indexed":
        return result

    item_id = str(result["item_id"])
    extracted_text = str(result.get("extracted_text") or "").strip()
    filename = str(result.get("filename") or Path(file_path).name)

    if result.get("enrichment_tier") == 1 and extracted_text:
        print(f"[ENRICH] scheduling background enrichment for {item_id}")
        asyncio.create_task(_background_enrich(item_id, extracted_text, filename))
        print(f"[ENRICH] pipeline continuing — enrichment is async for {item_id}")

    if result.get("handler_type") == "visual" and result.get("ocr_pending"):
        await _schedule_deferred_visual_ocr(item_id, path)

    await index_events.put(
        {
            "type": "file_indexed",
            "item_id": item_id,
            "title": result.get("title", filename),
            "file_type": result["file_type"],
            "handler_type": result["handler_type"],
        }
    )

    return {
        "status": "indexed",
        "item_id": item_id,
        "title": result.get("title"),
    }


async def index_file_safe(
    path: Path,
    force: bool = False,
    *,
    initial_scan: bool = False,
    use_semaphore: bool = True,
) -> dict[str, Any] | None:
    await _track_index_job_started()
    try:
        if use_semaphore:
            async with _semaphore:
                return await index_file(path, force=force, initial_scan=initial_scan)
        return await index_file(path, force=force, initial_scan=initial_scan)
    except Exception as exc:
        logger.warning("Failed to index %s: %s", Path(path).name, exc)
        return None
    finally:
        await _track_index_job_finished()


async def _background_enrich(item_id: str, text: str, filename: str) -> None:
    conn = None

    try:
        started_at = time.perf_counter()
        print(f"[ENRICH] background start for {item_id}")
        result = await enricher.enrich_item(item_id, text, filename)
        formatted = enricher.format_for_db(result)
        conn = database.get_connection()
        database.update_item(conn, item_id, formatted)
        item = database.get_item_by_id(conn, item_id)
        knowledge_event: dict[str, Any] = {
            "provider": "heuristic",
            "entities": 0,
            "temporal_facts": 0,
            "frame_type": None,
        }

        if item is not None:
            refreshed_item = dict(item)
            refreshed_item.update(formatted)
            knowledge = await extract_item_knowledge(refreshed_item)
            keyword_fields: dict[str, Any] = {}
            if knowledge.get("keywords"):
                keyword_fields["keywords"] = json.dumps(
                    list(knowledge["keywords"]),
                    ensure_ascii=True,
                )
            keyword_fields["enrichment_tier"] = max(
                int(refreshed_item.get("enrichment_tier") or 0),
                3,
            )
            if keyword_fields:
                database.update_item(conn, item_id, keyword_fields)
                refreshed_item.update(keyword_fields)
            knowledge_event = apply_item_knowledge(refreshed_item, knowledge)

        conn.close()
        conn = None
        elapsed = time.perf_counter() - started_at
        print(f"[ENRICH] background done for {item_id} in {elapsed:.3f}s")
        await index_events.put(
            {
                "type": "item_enriched",
                "item_id": item_id,
                "provider": knowledge_event.get("provider"),
                "frame_type": knowledge_event.get("frame_type"),
                "entities": knowledge_event.get("entities", 0),
                "temporal_facts": knowledge_event.get("temporal_facts", 0),
            }
        )
    except Exception as exc:
        logging.error("Enrichment failed for %s: %s", item_id, exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
