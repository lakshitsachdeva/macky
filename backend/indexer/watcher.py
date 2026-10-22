from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

try:
    from .. import config, database
    from . import pipeline, visual_embedder
except ImportError:
    import config
    import database
    from indexer import pipeline, visual_embedder


logger = logging.getLogger(__name__)
FAST_SKIP_FILE_SIZE_BYTES = 50 * 1024 * 1024
PENDING_RETRY_BATCH_LIMIT = 512
MACOS_DATALESS_FLAG = 0x40000000
_active_watcher: "MemoryEngineWatcher | None" = None
_background_scan_tasks: set[asyncio.Task[Any]] = set()
_worker_stats = {
    "active": 0,
    "completed": 0,
    "peak": 0,
}

EVENT_SKIP_PARTS = {
    ".git",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".turbo",
    ".yarn",
    "node_modules",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "target",
    "venv",
    ".venv",
    "site-packages",
    ".Trash",
    "Trash",
}

SCAN_SKIP_PARTS = {
    ".git",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".turbo",
    ".yarn",
    "node_modules",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "target",
    "venv",
    ".venv",
    "site-packages",
    ".Trash",
    "Trash",
}

SKIP_DIR_SUFFIXES = {
    ".app",
    ".bundle",
    ".egg-info",
    ".framework",
    ".plugin",
}

SCAN_PRIORITY_BY_SUFFIX = {
    ".pdf": 0,
    ".txt": 0,
    ".md": 0,
    ".docx": 0,
    ".doc": 0,
    ".rtf": 0,
    ".xlsx": 1,
    ".xls": 1,
    ".csv": 1,
    ".pptx": 1,
    ".ppt": 1,
    ".png": 2,
    ".jpg": 2,
    ".jpeg": 2,
    ".heic": 2,
    ".heif": 2,
    ".gif": 2,
    ".webp": 2,
}
SCAN_DISCOVERY_EMIT_INTERVAL = 200
SCAN_CHECK_EMIT_INTERVAL = 100
BACKGROUND_PDF_BYTES = 512 * 1024
BACKGROUND_WORD_DOC_BYTES = 2 * 1024 * 1024
BACKGROUND_TEXT_BYTES = 1 * 1024 * 1024
DEFERRED_INITIAL_CSV_BYTES = 12 * 1024 * 1024
DEFERRED_INITIAL_SPREADSHEET_BYTES = 16 * 1024 * 1024
DEFERRED_INITIAL_PRESENTATION_BYTES = 20 * 1024 * 1024
DEFERRED_INITIAL_PDF_BYTES = 24 * 1024 * 1024
DEFERRED_INITIAL_WORD_BYTES = 10 * 1024 * 1024
DEFERRED_INITIAL_TEXT_BYTES = 4 * 1024 * 1024
DEFERRED_INITIAL_IMAGE_BYTES = 10 * 1024 * 1024
INITIAL_BACKGROUND_ONLY_EXTENSIONS = {
    ".xlsx",
    ".xls",
    ".csv",
    ".tsv",
    ".pptx",
    ".ppt",
    ".key",
    ".pages",
    ".numbers",
    ".png",
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".mp4",
    ".mov",
    ".mp3",
    ".wav",
    ".m4a",
}
FAST_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
    ".docx",
    ".doc",
    ".rtf",
}
SLOW_EXTENSIONS = {
    ".xlsx",
    ".xls",
    ".csv",
    ".pptx",
    ".ppt",
    ".key",
    ".pages",
    ".numbers",
    ".png",
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".gif",
    ".webp",
    ".mp4",
    ".mov",
    ".mp3",
    ".wav",
    ".m4a",
}
NUM_FAST_WORKERS = 4
NUM_SLOW_WORKERS = 2
TERMINAL_WATCHED_STATUSES = {"failed", "missing", "skipped_age"}


def _requires_slow_visual_processing(
    path: Path,
    *,
    initial_scan: bool,
) -> bool:
    if path.suffix.lower() not in config.SUPPORTED_IMAGE_EXTENSIONS:
        return False

    if not visual_embedder.HAS_VISUAL_MODEL:
        return False

    should_process, _reason = visual_embedder.should_ocr(
        path,
        initial_scan=initial_scan,
    )
    return should_process


def _queue_for_path(path: Path, *, initial_scan: bool) -> str | None:
    suffix = path.suffix.lower()
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0

    if suffix in config.SUPPORTED_IMAGE_EXTENSIONS:
        return "slow" if _requires_slow_visual_processing(path, initial_scan=initial_scan) else "fast"

    if suffix in FAST_EXTENSIONS:
        if suffix in {".txt", ".md"} and size_bytes >= BACKGROUND_TEXT_BYTES:
            return "slow"
        if suffix == ".pdf" and size_bytes >= BACKGROUND_PDF_BYTES:
            return "slow"
        if suffix in {".docx", ".doc", ".rtf"} and size_bytes >= BACKGROUND_WORD_DOC_BYTES:
            return "slow"
        return "fast"

    if suffix in SLOW_EXTENSIONS:
        return "slow"

    return None


def _should_defer_initial_background(path: Path) -> bool:
    suffix = path.suffix.lower()
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return False

    if suffix in INITIAL_BACKGROUND_ONLY_EXTENSIONS:
        return True

    if suffix == ".csv":
        return size_bytes >= DEFERRED_INITIAL_CSV_BYTES

    if suffix in {".xlsx", ".xls"}:
        return size_bytes >= DEFERRED_INITIAL_SPREADSHEET_BYTES

    if suffix in {".pptx", ".ppt", ".key", ".pages", ".numbers"}:
        return size_bytes >= DEFERRED_INITIAL_PRESENTATION_BYTES

    if suffix == ".pdf":
        return size_bytes >= DEFERRED_INITIAL_PDF_BYTES

    if suffix in {".docx", ".doc", ".rtf"}:
        return size_bytes >= DEFERRED_INITIAL_WORD_BYTES

    if suffix in {".txt", ".md", ".json"}:
        return size_bytes >= DEFERRED_INITIAL_TEXT_BYTES

    if suffix in config.SUPPORTED_IMAGE_EXTENSIONS:
        return size_bytes >= DEFERRED_INITIAL_IMAGE_BYTES

    return False


def _drain_queue_nowait(queue: asyncio.Queue[Path]) -> Path | None:
    try:
        return queue.get_nowait()
    except asyncio.QueueEmpty:
        return None


async def _get_work_item(
    preferred_queue: asyncio.Queue[Path],
    preferred_lane: str,
    *,
    fallback_queue: asyncio.Queue[Path] | None = None,
    fallback_lane: str | None = None,
) -> tuple[Path, asyncio.Queue[Path], str]:
    preferred_now = _drain_queue_nowait(preferred_queue)
    if preferred_now is not None:
        return preferred_now, preferred_queue, preferred_lane

    if fallback_queue is not None and fallback_lane is not None:
        fallback_now = _drain_queue_nowait(fallback_queue)
        if fallback_now is not None:
            return fallback_now, fallback_queue, fallback_lane

    if fallback_queue is None or fallback_lane is None:
        path = await preferred_queue.get()
        return path, preferred_queue, preferred_lane

    preferred_task = asyncio.create_task(preferred_queue.get())
    fallback_task = asyncio.create_task(fallback_queue.get())
    done, pending = await asyncio.wait(
        {preferred_task, fallback_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    if preferred_task in done:
        return preferred_task.result(), preferred_queue, preferred_lane
    return fallback_task.result(), fallback_queue, fallback_lane


class MemoryEngineWatcher(FileSystemEventHandler):
    def __init__(
        self,
        watched_dirs: list[Path],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.watched_dirs = [path.expanduser().resolve() for path in watched_dirs]
        self.loop = loop
        self.observer = Observer()
        self.started = False
        self._debounce: dict[str, float] = {}
        self._scheduled: dict[str, object] = {}
        self._fast_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._slow_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._fast_tasks: list[asyncio.Task[Any]] = []
        self._slow_tasks: list[asyncio.Task[Any]] = []

    def start(self) -> None:
        global _active_watcher
        if self.started:
            return

        existing = [directory for directory in self.watched_dirs if directory.exists()]
        for directory in existing:
            self._schedule(directory)
        self.observer.start()
        self.started = True
        _active_watcher = self
        self._schedule_worker_pool_start()
        logging.info("Watching %s directories", len(existing))

    def stop(self) -> None:
        global _active_watcher
        self._schedule_worker_pool_stop()
        if self.started:
            self.observer.stop()
            self.observer.join()
            self.started = False
        self._scheduled.clear()
        if _active_watcher is self:
            _active_watcher = None

    def add_directory(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved not in self.watched_dirs:
            self.watched_dirs.append(resolved)
        if resolved.exists():
            self._schedule(resolved)

    def remove_directory(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        self.watched_dirs = [directory for directory in self.watched_dirs if directory != resolved]
        watch = self._scheduled.pop(str(resolved), None)
        if watch is not None:
            try:
                self.observer.unschedule(watch)
            except Exception:
                logger.debug("Unable to unschedule %s", resolved, exc_info=True)

    def _schedule(self, directory: Path) -> None:
        key = str(directory)
        if key in self._scheduled:
            return
        watch = self.observer.schedule(self, str(directory), recursive=True)
        self._scheduled[key] = watch

    def _schedule_worker_pool_start(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self._ensure_worker_pool_started)
        except RuntimeError:
            logger.debug("Event loop not available to start live worker pool", exc_info=True)

    def _schedule_worker_pool_stop(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self._cancel_worker_pool)
        except RuntimeError:
            self._cancel_worker_pool()

    def _ensure_worker_pool_started(self) -> None:
        self._fast_tasks = [task for task in self._fast_tasks if not task.done()]
        self._slow_tasks = [task for task in self._slow_tasks if not task.done()]
        if self._fast_tasks or self._slow_tasks:
            return

        self._fast_tasks = [
            self._track_worker_task(
                self.loop.create_task(
                    self._live_worker(
                        self._fast_queue,
                        "fast",
                        fallback_queue=self._slow_queue,
                        fallback_lane="slow",
                    ),
                    name=f"fast-worker-{index}",
                )
            )
            for index in range(NUM_FAST_WORKERS)
        ]
        self._slow_tasks = [
            self._track_worker_task(
                self.loop.create_task(
                    self._live_worker(self._slow_queue, "slow"),
                    name=f"slow-worker-{index}",
                )
            )
            for index in range(NUM_SLOW_WORKERS)
        ]

    def _cancel_worker_pool(self) -> None:
        tasks = [*self._fast_tasks, *self._slow_tasks]
        self._fast_tasks = []
        self._slow_tasks = []

        for task in tasks:
            task.cancel()

    def _track_worker_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        def _log_crash(done_task: asyncio.Task[Any]) -> None:
            if done_task.cancelled():
                return

            exception = done_task.exception()
            if exception is None:
                return

            logger.error(
                "Live worker %s crashed: %s",
                done_task.get_name(),
                exception,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

        task.add_done_callback(_log_crash)
        return task

    async def _enqueue_path(self, path: Path) -> None:
        self._ensure_worker_pool_started()

        lane = _queue_for_path(path, initial_scan=False)
        if lane == "slow":
            await self._slow_queue.put(path)
            return
        await self._fast_queue.put(path)

    async def _live_worker(
        self,
        queue: asyncio.Queue[Path],
        lane: str,
        *,
        fallback_queue: asyncio.Queue[Path] | None = None,
        fallback_lane: str | None = None,
    ) -> None:
        while True:
            path, active_queue, active_lane = await _get_work_item(
                queue,
                lane,
                fallback_queue=fallback_queue,
                fallback_lane=fallback_lane,
            )
            try:
                await pipeline.index_file_safe(
                    path,
                    initial_scan=False,
                    use_semaphore=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[Live %s worker] Error on %s: %s", active_lane, path, exc)
            finally:
                active_queue.task_done()

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return

        conn = database.get_connection()
        try:
            database.delete_watched_file(conn, Path(event.src_path))
        finally:
            conn.close()

    def on_moved(self, event) -> None:
        if event.is_directory:
            return

        conn = database.get_connection()
        try:
            database.delete_watched_file(conn, Path(event.src_path))
        finally:
            conn.close()

        self._handle(Path(event.dest_path))

    def _handle(self, path: Path) -> None:
        if not _should_index(path, EVENT_SKIP_PARTS):
            return

        now = time.time()
        path_key = str(path)
        if self._debounce.get(path_key, 0) > now - 5:
            return
        self._debounce[path_key] = now

        conn = database.get_connection()
        try:
            if not _is_new_or_changed(conn, path):
                return
        finally:
            conn.close()

        try:
            asyncio.run_coroutine_threadsafe(self._enqueue_path(path), self.loop)
        except RuntimeError:
            logger.debug("Unable to enqueue file event for %s", path, exc_info=True)


def _should_index(path: Path, skip_parts: set[str]) -> bool:
    return _scan_eligibility_reason(path, skip_parts) == "candidate"


def _scan_eligibility_reason(path: Path, skip_parts: set[str]) -> str:
    if path.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
        return "excluded"

    if path.name.startswith("."):
        return "excluded"

    if any(part in skip_parts for part in path.parts):
        return "excluded"

    if any(part.lower().endswith(suffix) for part in path.parts for suffix in SKIP_DIR_SUFFIXES):
        return "excluded"

    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return "excluded"

    if getattr(stat_result, "st_flags", 0) & MACOS_DATALESS_FLAG:
        return "excluded"

    size_bytes = stat_result.st_size

    if size_bytes > FAST_SKIP_FILE_SIZE_BYTES:
        return "too_large"

    if size_bytes == 0:
        return "excluded"

    if size_bytes > config.MAX_FILE_SIZE_MB * 1024 * 1024:
        return "too_large"

    return "candidate"


def _is_too_old_for_initial_scan(path: Path) -> bool:
    try:
        stat_result = path.stat()
    except OSError:
        return False

    if path.suffix.lower() in config.SUPPORTED_IMAGE_EXTENSIONS:
        return False

    age_days = (time.time() - stat_result.st_mtime) / 86400
    return age_days > config.MAX_FILE_AGE_DAYS


def _percent(count: int, total: int) -> int:
    if total <= 0:
        return 0
    return round((count / total) * 100)


def _stat_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return float(stat_result.st_mtime), int(stat_result.st_size)


def _track_background_scan_task(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _background_scan_tasks.add(task)

    def _cleanup(done_task: asyncio.Task[Any]) -> None:
        _background_scan_tasks.discard(done_task)

    task.add_done_callback(_cleanup)
    return task


def get_background_scan_task_count() -> int:
    return sum(1 for task in _background_scan_tasks if not task.done())


def _watched_row_matches(
    watched_row: dict[str, Any],
    stat_signature: tuple[float, int],
) -> bool:
    last_seen = watched_row.get("last_seen")
    file_size = watched_row.get("file_size")
    if last_seen is None or file_size is None:
        return False

    return (
        abs(float(last_seen) - float(stat_signature[0])) < 1e-6
        and int(file_size) == int(stat_signature[1])
    )


def _is_new_or_changed_preview(
    conn: Any,
    path: Path,
) -> bool:
    stat_signature = _stat_signature(path)
    if stat_signature is None:
        return False

    resolved_path = path.expanduser().resolve()
    watched_row = database.get_watched_file(conn, resolved_path)
    if watched_row is not None:
        if _watched_row_matches(watched_row, stat_signature):
            status = str(watched_row.get("status") or "").strip().casefold()
            item_id = str(watched_row.get("item_id") or "").strip()
            if status in {"indexed", "duplicate"}:
                return False
            if item_id:
                return False
            if database.get_item_by_path(conn, resolved_path) is not None:
                return False
            if status in TERMINAL_WATCHED_STATUSES:
                return False
            error_count = int(watched_row.get("error_count") or 0)
            if error_count >= database.MAX_WATCHED_FILE_ERRORS:
                return False
            last_attempted = watched_row.get("last_attempted")
            if last_attempted is not None:
                retry_cutoff = time.time() - database.WATCHED_FILE_RETRY_DELAY_SECONDS
                if float(last_attempted) >= retry_cutoff:
                    return False
            if status in {"pending", "error"}:
                return True
        return True

    return database.get_item_by_path(conn, resolved_path) is None


def _is_new_or_changed(
    conn: Any,
    path: Path,
) -> bool:
    stat_signature = _stat_signature(path)
    if stat_signature is None:
        return False

    resolved_path = path.expanduser().resolve()
    watched_row = database.get_watched_file(conn, resolved_path)
    if watched_row is not None:
        if _watched_row_matches(watched_row, stat_signature):
            status = str(watched_row.get("status") or "").strip().casefold()
            item_id = str(watched_row.get("item_id") or "").strip()
            if status in {"indexed", "duplicate"}:
                return False
            if item_id:
                database.upsert_watched_file(
                    conn,
                    resolved_path,
                    item_id=item_id,
                    last_seen=stat_signature[0],
                    file_size=stat_signature[1],
                    status="indexed",
                )
                return False

            existing = database.get_item_by_path(conn, resolved_path)
            if existing is not None:
                database.upsert_watched_file(
                    conn,
                    resolved_path,
                    item_id=str(existing["id"]),
                    last_seen=stat_signature[0],
                    file_size=stat_signature[1],
                    status="indexed",
                )
                return False
            if status in TERMINAL_WATCHED_STATUSES:
                return False
            error_count = int(watched_row.get("error_count") or 0)
            if error_count >= database.MAX_WATCHED_FILE_ERRORS:
                return False
            last_attempted = watched_row.get("last_attempted")
            if last_attempted is not None:
                retry_cutoff = time.time() - database.WATCHED_FILE_RETRY_DELAY_SECONDS
                if float(last_attempted) >= retry_cutoff:
                    return False
            if status in {"pending", "error"}:
                return True
        return True

    existing = database.get_item_by_path(conn, resolved_path)
    if existing is not None:
        database.upsert_watched_file(
            conn,
            resolved_path,
            item_id=str(existing["id"]),
            last_seen=stat_signature[0],
            file_size=stat_signature[1],
            status="indexed",
        )
        return False

    return True


async def _continue_deferred_initial_scan(deferred_files: list[Path]) -> int:
    if not deferred_files:
        return 0

    try:
        await _emit_scan_phase(
            "indexing",
            message=f"continuing {len(deferred_files)} heavier files in background...",
            total=len(deferred_files),
            new_found=len(deferred_files),
        )
        processed = await _process_scan_queue(deferred_files)
        await pipeline.index_events.put(
            {
                "type": "scan_complete",
                "indexed": processed,
                "processed": processed,
                "total": len(deferred_files),
                "percent": 100,
                "message": f"finished {processed} background files",
                "new_found": len(deferred_files),
            }
        )
        logger.info("Deferred background scan complete: %s files processed", processed)
        return processed
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Deferred background scan failed")
        return 0


def _scan_priority(path: Path) -> tuple[int, int, str]:
    suffix = path.suffix.lower()
    priority = SCAN_PRIORITY_BY_SUFFIX.get(suffix, 3)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    return priority, size_bytes, str(path).casefold()


def _queue_kind(path: Path) -> str | None:
    return _queue_for_path(path, initial_scan=False)


def _progress_message(path: Path, lane: str) -> str:
    suffix = path.suffix.lower()
    if lane == "fast":
        return f"indexing core documents first... {path.name}"
    if suffix in {".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".key", ".pages", ".numbers"}:
        return f"finishing spreadsheets and slides in the background... {path.name}"
    return f"indexing images in the background... {path.name}"


def load_watched_dirs() -> list[Path]:
    conn = database.get_connection()
    try:
        rows = database.get_watched_folders(conn)
    finally:
        conn.close()
    return [Path(str(row["path"])).expanduser() for row in rows]


def get_active_watcher() -> MemoryEngineWatcher | None:
    return _active_watcher


async def _emit_scan_phase(phase: str, **payload: object) -> None:
    await pipeline.index_events.put({"type": "scan_phase", "phase": phase, **payload})


async def _collect_scan_paths(watched_dirs: list[Path]) -> tuple[list[Path], dict[str, int]]:
    candidates: list[Path] = []
    summary = {
        "total_candidates": 0,
        "skipped_too_large": 0,
        "skipped_excluded": 0,
    }
    await _emit_scan_phase("discovering", message="discovering files...", found=0)

    for directory in watched_dirs:
        if not directory.exists():
            continue

        for root, dirs, files in os.walk(directory):
            dirs[:] = [
                name
                for name in dirs
                if (
                    name not in SCAN_SKIP_PARTS
                    and not name.startswith(".")
                    and not any(name.lower().endswith(suffix) for suffix in SKIP_DIR_SUFFIXES)
                )
            ]

            root_path = Path(root)
            if any(part in SCAN_SKIP_PARTS for part in root_path.parts):
                continue

            for filename in files:
                path = root_path / filename
                summary["total_candidates"] += 1
                reason = _scan_eligibility_reason(path, SCAN_SKIP_PARTS)
                if reason == "candidate":
                    candidates.append(path)
                    if len(candidates) % SCAN_DISCOVERY_EMIT_INTERVAL == 0:
                        await _emit_scan_phase(
                            "discovering",
                            message="discovering files...",
                            found=len(candidates),
                        )
                        await asyncio.sleep(0)
                elif reason == "too_large":
                    summary["skipped_too_large"] += 1
                else:
                    summary["skipped_excluded"] += 1

        await _emit_scan_phase(
            "discovering",
            message="discovering files...",
            found=len(candidates),
        )

    candidates.sort(key=_scan_priority)
    return candidates, summary


async def _filter_new_files(
    all_files: list[Path],
) -> tuple[list[Path], dict[str, int]]:
    await _emit_scan_phase(
        "checking",
        message=f"checking {len(all_files)} files...",
        checked=0,
        total=len(all_files),
        new_found=0,
    )
    new_files: list[Path] = []
    summary = {
        "skipped_unchanged": 0,
        "skipped_too_old": 0,
    }

    conn = database.get_connection()
    try:
        for index, path in enumerate(all_files, start=1):
            if _is_too_old_for_initial_scan(path):
                summary["skipped_too_old"] += 1
                continue

            if _is_new_or_changed(conn, path):
                new_files.append(path)
            else:
                summary["skipped_unchanged"] += 1

            if (
                index == 1
                or index == len(all_files)
                or index % SCAN_CHECK_EMIT_INTERVAL == 0
            ):
                await _emit_scan_phase(
                    "checking",
                    message=f"checking {index}/{len(all_files)} files...",
                    checked=index,
                    total=len(all_files),
                    new_found=len(new_files),
                )
                if index % (SCAN_CHECK_EMIT_INTERVAL * 5) == 0:
                    await asyncio.sleep(0)
    finally:
        conn.close()

    summary["queued_new"] = len(new_files)
    return new_files, summary


async def _process_scan_queue(
    new_files: list[Path],
) -> int:
    fast_queue: asyncio.Queue = asyncio.Queue()
    slow_queue: asyncio.Queue = asyncio.Queue()
    total = len(new_files)
    monitor_started_at = time.perf_counter()
    progress = {"processed": 0}
    progress_lock = asyncio.Lock()
    stats_lock = asyncio.Lock()

    _worker_stats["active"] = 0
    _worker_stats["completed"] = 0
    _worker_stats["peak"] = 0

    for path in new_files:
        lane = _queue_for_path(path, initial_scan=True)
        if lane == "slow":
            slow_queue.put_nowait(path)
        else:
            fast_queue.put_nowait(path)

    async def track_worker_start() -> None:
        async with stats_lock:
            _worker_stats["active"] += 1
            _worker_stats["peak"] = max(_worker_stats["peak"], _worker_stats["active"])

    async def track_worker_finish() -> None:
        async with stats_lock:
            _worker_stats["active"] = max(0, _worker_stats["active"] - 1)
            _worker_stats["completed"] += 1

    async def _concurrency_monitor() -> None:
        start = time.perf_counter()
        try:
            while True:
                await asyncio.sleep(5)
                elapsed = time.perf_counter() - start
                async with stats_lock:
                    active = _worker_stats["active"]
                    peak = _worker_stats["peak"]
                    completed = _worker_stats["completed"]
                rate = completed / elapsed if elapsed > 0 else 0.0
                print(
                    f"[CONCURRENCY] active={active}  "
                    f"peak={peak}  "
                    f"completed={completed}  "
                    f"rate={rate:.1f} files/sec"
                )
        except asyncio.CancelledError:
            return

    async def mark_processed(path: Path, lane: str) -> None:
        async with progress_lock:
            progress["processed"] += 1
            processed = progress["processed"]
            percent = round(processed / total * 100) if total else 100
            await pipeline.index_events.put(
                {
                    "type": "scan_progress",
                    "indexed": processed,
                    "processed": processed,
                    "total": total,
                    "remaining": max(total - processed, 0),
                    "percent": percent,
                    "message": _progress_message(path, lane),
                    "lane": lane,
                }
            )

            if processed % 20 == 0 or processed == total:
                logger.info("Indexed %s/%s files", processed, total)

    async def fast_worker() -> None:
        while True:
            path, active_queue, active_lane = await _get_work_item(
                fast_queue,
                "fast",
                fallback_queue=slow_queue,
                fallback_lane="slow",
            )
            await track_worker_start()
            try:
                await pipeline.index_file_safe(
                    path,
                    initial_scan=True,
                    use_semaphore=False,
                )
            except Exception as exc:
                logger.warning("[Initial %s worker] Error on %s: %s", active_lane, path, exc)
            finally:
                await track_worker_finish()
                active_queue.task_done()
                await mark_processed(path, active_lane)

    async def slow_worker() -> None:
        while True:
            path = await slow_queue.get()
            await track_worker_start()
            try:
                await pipeline.index_file_safe(
                    path,
                    initial_scan=True,
                    use_semaphore=False,
                )
            except Exception as exc:
                logger.warning("[Slow worker] Error on %s: %s", path, exc)
            finally:
                await track_worker_finish()
                slow_queue.task_done()
                await mark_processed(path, "slow")

    fast_tasks = [
        asyncio.create_task(fast_worker(), name=f"fast-worker-{index}")
        for index in range(NUM_FAST_WORKERS)
    ]
    slow_tasks = [
        asyncio.create_task(slow_worker(), name=f"slow-worker-{index}")
        for index in range(NUM_SLOW_WORKERS)
    ]
    monitor_task = asyncio.create_task(_concurrency_monitor())

    try:
        await fast_queue.join()
        await slow_queue.join()
    finally:
        for task in fast_tasks + slow_tasks:
            task.cancel()
        monitor_task.cancel()
        await asyncio.gather(*fast_tasks, *slow_tasks, return_exceptions=True)
        await asyncio.gather(monitor_task, return_exceptions=True)

    print(
        f"[CONCURRENCY] active={_worker_stats['active']}  "
        f"peak={_worker_stats['peak']}  "
        f"completed={_worker_stats['completed']}  "
        f"rate={(progress['processed'] / max(time.perf_counter() - monitor_started_at, 0.001)):.1f} files/sec"
    )

    return progress["processed"]


async def initial_scan(watched_dirs: list[Path] | None = None) -> int:
    """Walk all watched dirs, index everything. Called once on app startup."""
    if watched_dirs is None:
        watched_dirs = load_watched_dirs()

    logger.info("Starting initial scan across %s directories", len(watched_dirs))
    all_files, collect_summary = await _collect_scan_paths(watched_dirs)
    logger.info("Found %s files to index", len(all_files))

    new_files, filter_summary = await _filter_new_files(all_files)
    skipped = len(all_files) - len(new_files)
    logger.info(
        "%s new memories to index (%s files already accounted for)",
        len(new_files),
        skipped,
    )
    total_candidates = collect_summary["total_candidates"]
    print(f"[SCAN_SUMMARY] total={total_candidates}")
    print(
        "[SCAN_SUMMARY]   skipped_unchanged="
        f"{filter_summary['skipped_unchanged']} "
        f"({_percent(filter_summary['skipped_unchanged'], total_candidates)}%)"
    )
    print(
        "[SCAN_SUMMARY]   skipped_too_old="
        f"{filter_summary['skipped_too_old']} "
        "(skipped before queueing)"
    )
    print(
        "[SCAN_SUMMARY]   skipped_too_large="
        f"{collect_summary['skipped_too_large']}"
    )
    print(
        "[SCAN_SUMMARY]   skipped_excluded="
        f"{collect_summary['skipped_excluded']}"
    )
    print(
        "[SCAN_SUMMARY]   queued_new="
        f"{filter_summary['queued_new']} "
        f"({_percent(filter_summary['queued_new'], total_candidates)}%)"
    )
    new_files.sort(key=_scan_priority)
    foreground_files: list[Path] = []
    deferred_files: list[Path] = []
    for path in new_files:
        if _should_defer_initial_background(path):
            deferred_files.append(path)
        else:
            foreground_files.append(path)
    print(
        "[SCAN_SUMMARY]   deferred_background="
        f"{len(deferred_files)}"
    )

    conn = database.get_connection()
    try:
        database.sync_watched_files_for_scan(conn, watched_dirs, all_files)
        database.mark_watched_files_pending(conn, new_files)
    finally:
        conn.close()

    if deferred_files:
        logger.info(
            "Deferring %s files so the first scan can finish faster",
            len(deferred_files),
        )

    if not foreground_files:
        if not deferred_files:
            await pipeline.index_events.put(
                {
                    "type": "scan_complete",
                    "indexed": 0,
                    "total": 0,
                    "percent": 0,
                }
            )
            return 0
        return await _continue_deferred_initial_scan(deferred_files)

    await _emit_scan_phase(
        "indexing",
        message=(
            f"indexing your core documents now..."
            + (
                f" {len(deferred_files)} visual, spreadsheet, and oversized files will continue in background."
                if deferred_files
                else ""
            )
        ),
        total=len(foreground_files),
        new_found=len(foreground_files),
    )
    processed = await _process_scan_queue(foreground_files)

    await pipeline.index_events.put(
        {
            "type": "scan_complete",
            "indexed": processed,
            "total": len(foreground_files),
            "percent": 100,
        }
    )
    logger.info(
        "Initial scan complete: %s foreground files processed (%s deferred)",
        processed,
        len(deferred_files),
    )
    if deferred_files:
        _track_background_scan_task(
            asyncio.create_task(
                _continue_deferred_initial_scan(deferred_files),
                name="deferred-initial-scan",
            )
        )
    return processed


async def retry_pending_files(limit: int = PENDING_RETRY_BATCH_LIMIT) -> int:
    retry_cutoff = time.time() - database.WATCHED_FILE_RETRY_DELAY_SECONDS

    conn = database.get_connection()
    try:
        rows = database.get_retryable_pending_files(
            conn,
            retry_cutoff=retry_cutoff,
            immediate_small_file_bytes=visual_embedder.OCR_MIN_SIZE_BYTES,
            limit=limit,
        )
    finally:
        conn.close()

    retry_files: list[Path] = []
    seen_paths: set[str] = set()
    for row in rows:
        path_str = str(row.get("path") or "").strip()
        if not path_str:
            continue

        path = Path(path_str).expanduser()
        if not path.exists() or path.is_dir():
            continue

        resolved = path.resolve()
        resolved_str = str(resolved)
        if resolved_str in seen_paths:
            continue

        if _scan_eligibility_reason(resolved, SCAN_SKIP_PARTS) != "candidate":
            continue

        seen_paths.add(resolved_str)
        retry_files.append(resolved)

    if not retry_files:
        return 0

    retry_files.sort(key=_scan_priority)

    logger.info("Retrying %s pending files", len(retry_files))
    await _emit_scan_phase(
        "indexing",
        message=f"retrying {len(retry_files)} pending files...",
        total=len(retry_files),
        new_found=len(retry_files),
    )
    processed = await _process_scan_queue(retry_files)

    await pipeline.index_events.put(
        {
            "type": "scan_complete",
            "indexed": processed,
            "processed": processed,
            "total": len(retry_files),
            "percent": 100,
            "message": f"retried {processed} pending files",
            "new_found": len(retry_files),
        }
    )
    return processed
