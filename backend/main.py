from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _configure_packaged_offline_runtime() -> None:
    if not (getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")):
        return

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    warnings.filterwarnings("ignore", message=".*hf_xet.*")
    warnings.filterwarnings("ignore", message=".*Xet Storage.*")


_configure_packaged_offline_runtime()

try:
    from .auth import (
        DIRECT_ACCESS_CONFIGURED,
        LOCAL_MODE,
        REMOTE_ACCESS_CONFIGURED,
        get_access_info,
        get_current_user,
        require_access,
    )
    from . import config, database
    from . import custody
    from . import forensic
    from . import interactions as interactions_module
    from . import now_feed as now_feed_module
    from .cognition import bootstrap as cognitive_bootstrap
    from .cognition.production.rules import ProductionEngine
    from .cognition import store as cognitive_store
    from .cognition.activation import (
        compute_base_level_activation,
        normalize_activation,
        retrieval_probability,
    )
    from .connectors.gmail import (
        GmailClient,
        GmailSyncCancelledError,
        cleanup_gmail_items,
        sync_gmail,
    )
    from .graph import bootstrap as graph_bootstrap
    from .graph.store import get_store as get_graph_store
    from .indexer import embedder, pipeline, watcher
    from .indexer.folder_discovery import (
        get_cached_discovery,
        invalidate_discovery_cache,
    )
    from .indexer.visual_embedder import HAS_GEMINI_VISION, HAS_TESSERACT
    from .llm.local_llm import LocalLLM
    from .llm.synthesis import (
        synthesize_ask_answer,
        synthesize_community_briefing,
        synthesize_evolution_briefing,
        synthesize_proactive_briefing,
        synthesize_search_briefing,
        synthesize_temporal_briefing,
    )
    from .retrieval import explainer, query_parser, search
    from .retrieval.temporal import TemporalQueryEngine
    from . import personalize as personalize_module
except ImportError:
    from auth import (
        DIRECT_ACCESS_CONFIGURED,
        LOCAL_MODE,
        REMOTE_ACCESS_CONFIGURED,
        get_access_info,
        get_current_user,
        require_access,
    )
    import config
    import database
    import custody
    import forensic
    import interactions as interactions_module
    import now_feed as now_feed_module
    from cognition import bootstrap as cognitive_bootstrap
    from cognition.production.rules import ProductionEngine
    from cognition import store as cognitive_store
    from cognition.activation import (
        compute_base_level_activation,
        normalize_activation,
        retrieval_probability,
    )
    from connectors.gmail import (
        GmailClient,
        GmailSyncCancelledError,
        cleanup_gmail_items,
        sync_gmail,
    )
    from graph import bootstrap as graph_bootstrap
    from graph.store import get_store as get_graph_store
    from indexer import embedder, pipeline, watcher
    from indexer.folder_discovery import get_cached_discovery, invalidate_discovery_cache
    from indexer.visual_embedder import HAS_GEMINI_VISION, HAS_TESSERACT
    from llm.local_llm import LocalLLM
    from llm.synthesis import (
        synthesize_ask_answer,
        synthesize_community_briefing,
        synthesize_evolution_briefing,
        synthesize_proactive_briefing,
        synthesize_search_briefing,
        synthesize_temporal_briefing,
    )
    from retrieval import explainer, query_parser, search
    from retrieval.temporal import TemporalQueryEngine
    import personalize as personalize_module


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RECURDO_RERANKER_ENABLED = os.getenv("RECURDO_RERANKER_ENABLED", "1").strip() not in ("0", "false", "no", "off")
API_KEY_PATTERN = re.compile(r"^AIza[0-9A-Za-z_-]{20,120}$")
SAFE_ID_PATTERN = re.compile(r"^[0-9A-Za-z._:@/+\\-]{1,160}$")


class _WindowRateLimiter:
    """Small in-process sliding-window limiter for the local desktop API."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(math.ceil(window_seconds - (now - hits[0]))))
                return False, retry_after
            hits.append(now)
            if len(self._hits) > 10000:
                self._prune(cutoff)
            return True, 0

    def _prune(self, cutoff: float) -> None:
        stale_keys = []
        for key, hits in self._hits.items():
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                stale_keys.append(key)
        for key in stale_keys:
            self._hits.pop(key, None)


_rate_limiter = _WindowRateLimiter()


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()[:80]
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()[:80]
    return (request.client.host if request.client else "unknown")[:80]


def _jwt_subject_from_request(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    payload = token.split(".")[1] if "." in token else ""
    if not payload:
        return None
    try:
        padded = payload + "=" * ((4 - len(payload) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")))
    except Exception:
        return None
    subject = claims.get("sub") or claims.get("user_id") or claims.get("email")
    return str(subject).strip()[:160] if subject else None


def _limits_for_request(request: Request) -> tuple[tuple[int, int], tuple[int, int] | None]:
    path = request.url.path
    method = request.method.upper()
    if path == "/health":
        return (300, 60), None
    if path in {"/ask", "/search", "/search/briefing"}:
        return (120, 60), (45, 60)
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return (120, 60), (60, 60)
    return (300, 60), (180, 60)


def _rate_limit_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": {
                "code": "RATE_LIMITED",
                "message": "Too many requests. Please wait a moment and try again.",
                "retry_after_seconds": retry_after,
            }
        },
        headers={"Retry-After": str(retry_after)},
    )

_watcher: watcher.MemoryEngineWatcher | None = None
_is_scanning: bool = False
_websocket_clients: set[WebSocket] = set()
_scan_indexed: int = 0
_scan_total: int = 0
_scan_percent: int = 0
_scan_start_count: int = 0
_scan_phase_state: dict[str, Any] = {
    "phase": "",
    "message": "",
    "found": 0,
    "checked": 0,
    "total": 0,
    "new_found": 0,
}
_startup_health_state: dict[str, Any] | None = None
_background_tasks: set[asyncio.Task[Any]] = set()
PENDING_RETRY_POLL_SECONDS = 5
KEEPALIVE_INTERVAL_SECONDS = 5 * 60
MODEL_IDLE_WARMUP_SECONDS = 10 * 60
HEALTH_DEGRADED_IDLE_SECONDS = 60 * 60
_server_started_at: float = time.time()
_last_query_time: float = _server_started_at
_keepalive_state: dict[str, Any] = {
    "last_tick_started_at": None,
    "last_success_at": None,
    "last_failure_at": None,
    "last_error": None,
    "reload_attempted_at": None,
}
_cognitive_bootstrap_task: asyncio.Task[Any] | None = None
_cognitive_bootstrap_state: dict[str, Any] = {
    "state": "idle",
    "phase": "",
    "message": "",
    "processed": 0,
    "total": 0,
    "percent": 0.0,
    "dry_run": False,
    "reset": False,
    "started_at": None,
    "completed_at": None,
    "error": None,
    "summary": {},
}
_gmail_oauth_task: asyncio.Task[Any] | None = None
_gmail_sync_task: asyncio.Task[Any] | None = None
_gmail_sync_cancel_event: threading.Event | None = None
_gmail_oauth_state: dict[str, Any] = {
    "state": "idle",
    "phase": "",
    "message": "",
    "processed": 0,
    "total": 0,
    "percent": 0.0,
    "started_at": None,
    "completed_at": None,
    "error": None,
    "summary": {},
}
_gmail_sync_state: dict[str, Any] = {
    "state": "idle",
    "phase": "",
    "message": "",
    "processed": 0,
    "total": 0,
    "percent": 0.0,
    "started_at": None,
    "completed_at": None,
    "error": None,
    "summary": {},
}


async def _warm_backend_assets() -> None:
    try:
        await pipeline.ensure_model_loaded()
        await asyncio.to_thread(embedder.get_text_index)
        logger.info("Backend warmup finished")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Backend warmup failed")


async def _start_live_watcher(live_watcher: watcher.MemoryEngineWatcher) -> None:
    try:
        await asyncio.to_thread(live_watcher.start)
        logger.info("Live watcher ready")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Live watcher failed to start")


def _track_background_task(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _background_tasks.add(task)

    def _cleanup(done_task: asyncio.Task[Any]) -> None:
        _background_tasks.discard(done_task)

    task.add_done_callback(_cleanup)
    return task


def _get_item_count() -> int:
    return int(database.run_with_connection_retry(database.get_item_count))


def _get_count_summary() -> dict[str, int]:
    return dict(database.run_with_connection_retry(database.get_count_summary))


def _get_pending_watched_file_count() -> int:
    return int(database.run_with_connection_retry(database.get_pending_watched_file_count))


def _get_item_record(item_id: str) -> dict[str, Any] | None:
    conn = database.get_connection()
    try:
        return database.get_item_by_id(conn, item_id)
    finally:
        conn.close()


def _get_item_records(item_ids: list[str]) -> list[dict[str, Any]]:
    unique_item_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
    if not unique_item_ids:
        return []
    conn = database.get_connection()
    try:
        rows: list[dict[str, Any]] = []
        for item_id in unique_item_ids:
            row = database.get_item_by_id(conn, item_id)
            if row is not None:
                rows.append(row)
        return rows
    finally:
        conn.close()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_query_activity() -> None:
    global _last_query_time
    _last_query_time = time.time()


def _keepalive_db_ping_sync() -> None:
    def _ping(conn: sqlite3.Connection) -> None:
        conn.execute("SELECT 1").fetchone()

    database.run_with_connection_retry(_ping)


def _keepalive_faiss_ping_sync() -> int:
    text_index = embedder.get_text_index()
    text_index.search(np.ones(text_index.dim, dtype=np.float32), k=5)
    return int(text_index.total)


def _reload_text_index_sync() -> int:
    text_index = embedder.get_text_index()
    text_index.load()
    return int(text_index.total)


def _get_db_item_count() -> int:
    def _count(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS count FROM items").fetchone()
        return int(row["count"] if row is not None else 0)

    return int(database.run_with_connection_retry(_count))


def _get_keepalive_snapshot(now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    snapshot = dict(_keepalive_state)

    def _age_for(value: Any) -> int | None:
        if value is None:
            return None
        return max(0, int(current - float(value)))

    snapshot["last_tick_age_s"] = _age_for(snapshot.get("last_tick_started_at"))
    snapshot["last_success_age_s"] = _age_for(snapshot.get("last_success_at"))
    snapshot["last_failure_age_s"] = _age_for(snapshot.get("last_failure_at"))
    return snapshot


def _compute_health_payload(now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    issues: list[str] = []

    try:
        summary = _get_count_summary()
    except Exception as exc:
        logger.warning("Health summary query failed: %s", exc)
        summary = {"unique_memories": 0, "files_watched": 0, "duplicates_skipped": 0}
        issues.append(f"summary:{exc}")

    try:
        db_items = _get_db_item_count()
    except Exception as exc:
        logger.warning("Health DB count failed: %s", exc)
        db_items = 0
        issues.append(f"db_items:{exc}")

    try:
        pending_files = _get_pending_watched_file_count()
    except Exception as exc:
        logger.warning("Health pending-file query failed: %s", exc)
        pending_files = 0
        issues.append(f"pending_files:{exc}")
    scan_phase, progress, is_scanning = _status_scan_snapshot(summary, pending_files)
    model_pool = embedder.get_model_pool_status()
    cognitive_status = _get_cognitive_status_snapshot()
    graph_status = _get_graph_status_snapshot()
    llm_status = _get_llm_status_snapshot()
    gmail_status = _get_gmail_status_snapshot()

    faiss_items: int | None = None
    try:
        faiss_items = int(_keepalive_faiss_ping_sync())
    except Exception as exc:
        logger.warning("Health FAISS probe failed: %s", exc)
        issues.append(f"faiss:{exc}")

    last_query_age_s = max(0, int(current - _last_query_time))
    uptime_s = max(0, int(current - _server_started_at))
    keepalive = _get_keepalive_snapshot(current)
    last_keepalive_success_age = keepalive.get("last_success_age_s")
    degraded = (
        faiss_items is None
        or (
            last_query_age_s > HEALTH_DEGRADED_IDLE_SECONDS
            and (
                last_keepalive_success_age is None
                or last_keepalive_success_age > HEALTH_DEGRADED_IDLE_SECONDS
            )
        )
    )

    return {
        "status": "degraded" if degraded else "ok",
        "issues": issues,
        "indexed_items": summary["unique_memories"],
        "unique_memories": summary["unique_memories"],
        "files_watched": summary["files_watched"],
        "duplicates_skipped": summary["duplicates_skipped"],
        "pending_files": pending_files,
        "scan_progress": progress,
        "scan_phase": scan_phase,
        "is_scanning": is_scanning,
        "text_model": "nomic-embed-text-v1.5",
        "model_loaded": bool(model_pool["initialized"]),
        "model_pool_ready": bool(model_pool["initialized"]),
        "model_pool_size": int(model_pool["pool_size"]),
        "model_pool_requested_size": int(model_pool["requested_pool_size"]),
        "query_model_pool_ready": bool(model_pool["query_pool_initialized"]),
        "query_model_pool_size": int(model_pool["query_pool_size"]),
        "query_model_pool_requested_size": int(
            model_pool["query_pool_requested_size"]
        ),
        "faiss_items": int(faiss_items or 0),
        "db_items": db_items,
        "last_query_age_s": last_query_age_s,
        "uptime_s": uptime_s,
        "keepalive": keepalive,
        "cognitive_layer_ready": cognitive_status["ready"],
        "cognitive_activation_events": cognitive_status["activation_events"],
        "cognitive_entity_edges": cognitive_status["entity_edges"],
        "cognitive_frame_instances": cognitive_status["frame_instances"],
        "cognitive_db_path": str(config.COGNITIVE_DB_PATH),
        "cognitive": cognitive_status,
        "graph_layer_ready": graph_status["ready"],
        "graph_entities": graph_status["entities"],
        "graph_episode_mentions": graph_status["episode_mentions"],
        "graph_temporal_facts": graph_status["temporal_facts"],
        "graph_db_path": str(config.GRAPH_DB_PATH),
        "graph": graph_status,
        "llm": llm_status,
        "gmail": gmail_status,
        "ocr_available": HAS_TESSERACT,
        "vision_available": HAS_GEMINI_VISION,
        "enrichment_available": bool(config.GEMINI_API_KEY and config.HAS_GEMINI_SDK),
    }


async def _run_keepalive_loop() -> None:
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
        started_at = time.time()
        _keepalive_state["last_tick_started_at"] = started_at
        try:
            await asyncio.to_thread(_keepalive_db_ping_sync)
            await asyncio.to_thread(_keepalive_faiss_ping_sync)
            if started_at - _last_query_time > MODEL_IDLE_WARMUP_SECONDS:
                await pipeline.ensure_model_loaded()
                await asyncio.to_thread(embedder.embed_query, "recurdo keepalive warmup")
            _keepalive_state["last_success_at"] = time.time()
            _keepalive_state["last_error"] = None
            logger.info(
                "keepalive ok [%s]",
                datetime.now().astimezone().strftime("%H:%M:%S"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _keepalive_state["last_failure_at"] = time.time()
            _keepalive_state["last_error"] = str(exc)
            logger.warning("keepalive FAILED: %s", exc)
            try:
                _keepalive_state["reload_attempted_at"] = time.time()
                await asyncio.to_thread(_reload_text_index_sync)
            except Exception:
                logger.exception("Unable to reload FAISS index after keepalive failure")


def _blank_cognitive_bootstrap_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "phase": "",
        "message": "",
        "processed": 0,
        "total": 0,
        "percent": 0.0,
        "dry_run": False,
        "reset": False,
        "started_at": None,
        "completed_at": None,
        "error": None,
        "summary": {},
    }


def _update_cognitive_bootstrap_state(event: dict[str, Any]) -> dict[str, Any]:
    global _cognitive_bootstrap_state

    current = dict(_cognitive_bootstrap_state)
    next_state = {
        **current,
        "state": str(event.get("state") or current.get("state") or "idle"),
        "phase": str(event.get("phase") or current.get("phase") or ""),
        "message": str(event.get("message") or current.get("message") or ""),
        "processed": int(event.get("processed", current.get("processed", 0)) or 0),
        "total": int(event.get("total", current.get("total", 0)) or 0),
        "percent": float(event.get("percent", current.get("percent", 0.0)) or 0.0),
        "dry_run": bool(event.get("dry_run", current.get("dry_run", False))),
        "reset": bool(event.get("reset", current.get("reset", False))),
        "started_at": event.get("started_at", current.get("started_at")),
        "completed_at": event.get("completed_at", current.get("completed_at")),
        "error": event.get("error", current.get("error")),
        "summary": dict(event.get("summary") or current.get("summary") or {}),
    }

    if next_state["state"] == "running":
        next_state["started_at"] = next_state.get("started_at") or _utc_now_iso()
        next_state["completed_at"] = None
        next_state["error"] = None
    elif next_state["state"] == "completed":
        next_state["completed_at"] = next_state.get("completed_at") or _utc_now_iso()
        next_state["error"] = None
    elif next_state["state"] == "error":
        next_state["completed_at"] = next_state.get("completed_at") or _utc_now_iso()

    _cognitive_bootstrap_state = next_state
    return dict(_cognitive_bootstrap_state)


def _emit_cognitive_bootstrap_event_sync(event: dict[str, Any]) -> None:
    payload = {
        "type": "cognitive_bootstrap",
        **event,
    }
    _update_cognitive_bootstrap_state(payload)
    asyncio.create_task(_broadcast_event(payload))


def _get_cognitive_status_snapshot() -> dict[str, Any]:
    counts = {
        "activation_events": 0,
        "entity_edges": 0,
        "frame_instances": 0,
    }
    ready = False
    db_path = Path(config.COGNITIVE_DB_PATH).expanduser()
    if db_path.exists():
        try:
            with sqlite3.connect(str(db_path), timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM activation_history) AS activation_events,
                        (SELECT COUNT(*) FROM entity_network) AS entity_edges,
                        (SELECT COUNT(*) FROM frame_instances) AS frame_instances
                    """
                ).fetchone()
            if row is not None:
                counts = {
                    "activation_events": int(row["activation_events"] or 0),
                    "entity_edges": int(row["entity_edges"] or 0),
                    "frame_instances": int(row["frame_instances"] or 0),
                }
            ready = True
        except Exception:
            logger.exception("Failed to read cognitive layer status")

    return {
        "ready": ready,
        "db_path": str(db_path),
        **counts,
        "bootstrap": dict(_cognitive_bootstrap_state),
    }


def _get_graph_status_snapshot() -> dict[str, Any]:
    db_path = Path(config.GRAPH_DB_PATH).expanduser()
    ready = False
    backend_name = str(getattr(config, "GRAPH_STORE_BACKEND", "sqlite") or "sqlite")
    counts = {
        "entities": 0,
        "episode_mentions": 0,
        "temporal_facts": 0,
    }

    if db_path.exists():
        try:
            store = get_graph_store()
            backend_name = str(getattr(store, "backend_name", backend_name))
            counts = store.get_counts()
            ready = True
        except Exception:
            logger.exception("Failed to read graph layer status")

    return {
        "ready": ready,
        "backend": backend_name,
        "db_path": str(db_path),
        **counts,
    }


def _get_llm_status_snapshot() -> dict[str, Any]:
    base = LocalLLM("quality").status()
    base["gemini_vision_enabled"] = bool(getattr(config, "GEMINI_VISION_ENABLED", False))
    base["excluded_dirs"] = [str(p) for p in getattr(config, "EXCLUDED_DIRS", [])]
    return base


def _blank_gmail_job_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "phase": "",
        "message": "",
        "processed": 0,
        "total": 0,
        "percent": 0.0,
        "started_at": None,
        "completed_at": None,
        "error": None,
        "summary": {},
    }


def _update_gmail_job_state(kind: str, event: dict[str, Any]) -> dict[str, Any]:
    global _gmail_oauth_state, _gmail_sync_state

    current = dict(_gmail_oauth_state if kind == "oauth" else _gmail_sync_state)
    next_state = {
        **current,
        "state": str(event.get("state") or current.get("state") or "idle"),
        "phase": str(event.get("phase") or current.get("phase") or ""),
        "message": str(event.get("message") or current.get("message") or ""),
        "processed": int(event.get("processed", current.get("processed", 0)) or 0),
        "total": int(event.get("total", current.get("total", 0)) or 0),
        "percent": float(event.get("percent", current.get("percent", 0.0)) or 0.0),
        "started_at": event.get("started_at", current.get("started_at")),
        "completed_at": event.get("completed_at", current.get("completed_at")),
        "error": event.get("error", current.get("error")),
        "summary": dict(event.get("summary") or current.get("summary") or {}),
    }

    if next_state["state"] == "running":
        next_state["started_at"] = next_state.get("started_at") or _utc_now_iso()
        next_state["completed_at"] = None
        next_state["error"] = None
    elif next_state["state"] == "completed":
        next_state["completed_at"] = next_state.get("completed_at") or _utc_now_iso()
        next_state["error"] = None
    elif next_state["state"] == "error":
        next_state["completed_at"] = next_state.get("completed_at") or _utc_now_iso()

    if kind == "oauth":
        _gmail_oauth_state = next_state
    else:
        _gmail_sync_state = next_state
    return dict(next_state)


def _emit_gmail_event_sync(kind: str, event: dict[str, Any]) -> None:
    payload = {
        "type": f"gmail_{kind}",
        **event,
    }
    _update_gmail_job_state(kind, payload)
    asyncio.create_task(_broadcast_event(payload))


def _get_gmail_item_count() -> int:
    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM items
            WHERE COALESCE(is_excluded, 0) = 0
              AND source_type IN ('gmail', 'gmail_attachment')
            """
        ).fetchone()
    finally:
        conn.close()
    return int(row["count"] or 0) if row is not None else 0


def _get_gmail_status_snapshot() -> dict[str, Any]:
    client = GmailClient()
    try:
        base_status = client.status()
    except Exception as exc:
        base_status = {
            "configured": client.client_configured(),
            "oauthlib_available": False,
            "connected": False,
            "client_secrets_path": str(client.client_secrets_path),
            "token_path": str(client.token_path),
            "sync_state_path": str(client.sync_state_path),
            "account_email": "",
            "last_history_id": "",
            "last_sync_at": None,
            "connected_at": None,
            "last_summary": {},
            "policy": {},
            "error": str(exc),
        }

    base_status["indexed_items"] = _get_gmail_item_count()
    base_status["oauth"] = dict(_gmail_oauth_state)
    base_status["sync"] = dict(_gmail_sync_state)
    return base_status


async def _run_gmail_oauth() -> None:
    loop = asyncio.get_running_loop()
    _emit_gmail_event_sync(
        "oauth",
        {
            "state": "running",
            "phase": "authenticating",
            "message": "opening Gmail authentication in your browser",
            "processed": 0,
            "total": 0,
            "percent": 0.0,
            "summary": {},
        },
    )
    try:
        summary = await asyncio.to_thread(GmailClient().authenticate)
        _emit_gmail_event_sync(
            "oauth",
            {
                "state": "completed",
                "phase": "complete",
                "message": "gmail connected",
                "processed": 1,
                "total": 1,
                "percent": 100.0,
                "summary": summary,
                "completed_at": _utc_now_iso(),
            },
        )
    except Exception as exc:
        logger.exception("Gmail authentication failed")
        _emit_gmail_event_sync(
            "oauth",
            {
                "state": "error",
                "phase": "authenticating",
                "message": f"gmail authentication failed: {exc}",
                "error": str(exc),
                "completed_at": _utc_now_iso(),
                "summary": {},
            },
        )


def _start_gmail_oauth_task() -> str:
    global _gmail_oauth_task
    if _gmail_oauth_task is not None and not _gmail_oauth_task.done():
        return "already_running"

    _gmail_oauth_state.update(_blank_gmail_job_state())
    _gmail_oauth_task = _track_background_task(asyncio.create_task(_run_gmail_oauth()))

    def _cleanup(done_task: asyncio.Task[Any]) -> None:
        global _gmail_oauth_task
        if _gmail_oauth_task is done_task:
            _gmail_oauth_task = None

    _gmail_oauth_task.add_done_callback(_cleanup)
    return "started"


async def _run_gmail_sync(
    *,
    full_sync: bool,
    historical_days: int | None,
    require_attachments: bool,
    max_messages: int | None,
) -> None:
    global _gmail_sync_cancel_event
    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()
    _gmail_sync_cancel_event = cancel_event

    def emit_progress(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(_emit_gmail_event_sync, "sync", event)

    _emit_gmail_event_sync(
        "sync",
        {
            "state": "running",
            "phase": "starting",
            "message": "starting gmail sync",
            "processed": 0,
            "total": 0,
            "percent": 0.0,
            "summary": {
                "full_sync": bool(full_sync),
                "historical_days": historical_days,
                "require_attachments": bool(require_attachments),
                "max_messages": max_messages,
            },
        },
    )

    try:
        summary = await asyncio.to_thread(
            sync_gmail,
            full_sync=bool(full_sync),
            historical_days=historical_days,
            require_attachments=bool(require_attachments),
            max_messages=max_messages,
            progress_callback=emit_progress,
            cancel_requested=cancel_event.is_set,
        )
        _emit_gmail_event_sync(
            "sync",
            {
                "state": "completed",
                "phase": "complete",
                "message": "gmail sync complete",
                "processed": int(summary.get("processed") or 0),
                "total": int(summary.get("message_ids") or summary.get("processed") or 0),
                "percent": 100.0,
                "summary": summary,
                "completed_at": _utc_now_iso(),
            },
        )
    except GmailSyncCancelledError:
        _emit_gmail_event_sync(
            "sync",
            {
                "state": "cancelled",
                "phase": "stopped",
                "message": "gmail sync stopped",
                "completed_at": _utc_now_iso(),
                "summary": {
                    "full_sync": bool(full_sync),
                    "historical_days": historical_days,
                    "require_attachments": bool(require_attachments),
                    "max_messages": max_messages,
                },
            },
        )
    except Exception as exc:
        logger.exception("Gmail sync failed")
        _emit_gmail_event_sync(
            "sync",
            {
                "state": "error",
                "phase": "syncing",
                "message": f"gmail sync failed: {exc}",
                "error": str(exc),
                "completed_at": _utc_now_iso(),
                "summary": {
                    "full_sync": bool(full_sync),
                    "historical_days": historical_days,
                    "require_attachments": bool(require_attachments),
                    "max_messages": max_messages,
                },
            },
        )
    finally:
        if _gmail_sync_cancel_event is cancel_event:
            _gmail_sync_cancel_event = None


def _start_gmail_sync_task(
    *,
    full_sync: bool,
    historical_days: int | None,
    require_attachments: bool,
    max_messages: int | None,
) -> str:
    global _gmail_sync_task
    if _gmail_sync_task is not None and not _gmail_sync_task.done():
        return "already_running"

    _gmail_sync_state.update(_blank_gmail_job_state())
    _gmail_sync_task = _track_background_task(
        asyncio.create_task(
            _run_gmail_sync(
                full_sync=full_sync,
                historical_days=historical_days,
                require_attachments=require_attachments,
                max_messages=max_messages,
            )
        )
    )

    def _cleanup(done_task: asyncio.Task[Any]) -> None:
        global _gmail_sync_task
        if _gmail_sync_task is done_task:
            _gmail_sync_task = None

    _gmail_sync_task.add_done_callback(_cleanup)
    return "started"


def _stop_gmail_sync_task() -> str:
    if _gmail_sync_task is None or _gmail_sync_task.done():
        return "idle"

    if _gmail_sync_cancel_event is not None:
        _gmail_sync_cancel_event.set()

    _emit_gmail_event_sync(
        "sync",
        {
            "state": "cancelling",
            "phase": "stopping",
            "message": "stopping gmail sync",
        },
    )
    return "stopping"


def _get_temporal_engine() -> TemporalQueryEngine:
    return TemporalQueryEngine(
        graph_store=get_graph_store(),
        memory_db_path=config.DB_PATH,
    )


SEARCH_RESPONSE_ALLOWED_FIELDS = {
    "id",
    "title",
    "filename",
    "file_type",
    "handler_type",
    "source_type",
    "original_path",
    "preview_text",
    "summary",
    "extracted_text",   # included for /ask synthesis — clipped to 1200 chars in render
    "created_at",
    "modified_at",
    "first_seen_at",
    "last_opened_at",
    "sender_name",
    "open_count",
    "importance_score",
    "file_size_bytes",
    "case_ids",
    "evidence_sha256",
    "evidence_size_bytes",
    "evidence_mtime_ns",
    "acquisition_status",
}


def _serialize_search_results(
    results: list[Any],
    *,
    parsed: query_parser.ParsedQuery,
    limit: int,
    explain: bool = False,
) -> list[dict[str, Any]]:
    provenance_by_item: dict[str, dict[str, Any]] = {}
    item_ids = [
        str(getattr(result, "item_id", "") or "")
        for result in results
        if str(getattr(result, "item_id", "") or "")
    ]
    if item_ids:
        try:
            conn = database.get_connection()
            try:
                provenance_by_item = database.get_item_provenance(conn, item_ids)
            finally:
                conn.close()
        except Exception:
            logger.debug("Unable to load search-result provenance", exc_info=True)

    response: list[dict[str, Any]] = []
    for result in results:
        if not _item_exists_on_disk(result.item):
            continue
        item = {
            **result.item,
            **provenance_by_item.get(str(result.item_id), {}),
        }
        payload = {
            **{
                key: value
                for key, value in item.items()
                if key in SEARCH_RESPONSE_ALLOWED_FIELDS
            },
            "base_score": round(result.base_score or result.final_score, 3),
            "cognitive_score": round(result.cognitive_score or result.final_score, 3),
            "final_score": round(result.final_score, 3),
            "cognitive_breakdown": result.cognitive_breakdown,
            "frame_type": result.frame_type,
            "intent_class": result.intent_class,
            "source_pool": result.source_pool,
            "graph_path": result.graph_path,
            "complexity_score": round(float(result.complexity_score or 0.0), 3),
            "pipeline": result.pipeline,
            "use_graph": bool(result.use_graph),
            "use_temporal_subgraph": bool(result.use_temporal_subgraph),
            "explanation": explainer.explain_match(result, parsed),
        }
        if explain:
            payload.update(
                {
                    "dense_score": round(float(result.dense_score or 0.0), 3),
                    "sparse_score": round(float(result.sparse_score or 0.0), 3),
                    "fts_score": round(float(result.fts_score or 0.0), 3),
                    "filename_score": round(float(result.filename_score or 0.0), 3),
                    "time_score": round(float(result.time_score or 0.0), 3),
                    "metadata_score": round(float(result.metadata_score or 0.0), 3),
                    "behavior_score": round(float(result.behavior_score or 0.0), 3),
                    "recency_score": round(float(result.recency_score or 0.0), 3),
                    "graph_score": round(float(result.graph_score or 0.0), 3),
                    "matched_fields": list(result.matched_fields),
                }
            )
        response.append(payload)
        if len(response) >= limit:
            break
    return response


def _is_cognitive_bootstrap_running() -> bool:
    return _cognitive_bootstrap_task is not None and not _cognitive_bootstrap_task.done()


def _get_context_snapshot(
    *,
    current_item_id: str | None = None,
    current_query: str | None = None,
) -> dict[str, Any]:
    engine = ProductionEngine(
        cognitive_store.get_store(),
        memory_db_path=config.DB_PATH,
    )
    context = engine.build_context(
        current_item_id=current_item_id,
        current_query=current_query,
    )
    rules = engine.evaluate(context, log_fires=False)

    current_item = context.get("current_item") or {}
    current_item_summary = None
    if current_item_id:
        current_item_summary = {
            "item_id": current_item_id,
            "title": current_item.get("title") or current_item.get("filename") or current_item_id,
            "source_type": current_item.get("source_type"),
            "file_type": current_item.get("file_type"),
        }

    return {
        "generated_at": _utc_now_iso(),
        "rules": rules,
        "context": {
            "current_item": current_item_summary,
            "current_frame": context.get("current_frame"),
            "active_script": context.get("active_script"),
            "script_name": context.get("script_name"),
            "current_scene": context.get("current_scene"),
            "query_scene": context.get("query_scene"),
            "next_scene": context.get("next_scene"),
            "script_confidence": context.get("confidence", 0.0),
            "script_evidence": context.get("evidence", []),
            "days_since_script_activity": context.get("days_since_script_activity", 0),
            "file_age_hours": context.get("file_age_hours", 999.0),
            "spreading_hit_count": context.get("spreading_hit_count", 0),
            "unanswered_email_count": len(context.get("unanswered_emails", [])),
            "unpaid_invoice_count": len(context.get("unpaid_invoices", [])),
            "invoice_age_days": context.get("invoice_age_days", 0),
            "current_query": current_query or "",
        },
    }


def _get_watched_folder_paths() -> list[Path]:
    conn = database.get_connection()
    try:
        watched = database.get_watched_folders(conn)
    finally:
        conn.close()
    return [Path(str(folder["path"])).expanduser() for folder in watched]


def _auto_index_on_start() -> bool:
    """Whether persisted watched folders may start work during boot.

    SSD/local recovery launches intentionally disable this. A copied library
    can contain broad legacy watched folders, and auto-scanning them while the
    API is starting makes SQLite writes contend with search requests. Users
    can still explicitly add a folder or press the index trigger.
    """
    return str(os.getenv("RECURDO_AUTO_INDEX_ON_START", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _current_scan_progress(unique_memories: int | None = None) -> dict[str, Any]:
    indexed_progress = _scan_indexed
    percent = float(_scan_percent)

    current_unique_memories = unique_memories if unique_memories is not None else _get_item_count()
    if _is_scanning and _scan_total:
        indexed_progress = max(indexed_progress, max(0, current_unique_memories - _scan_start_count))
        percent = min(100.0, max(percent, round((indexed_progress / _scan_total) * 100, 1)))

    remaining = max(0, _scan_total - indexed_progress)

    return {
        "phase": str(_scan_phase_state.get("phase") or ("indexing" if _is_scanning else "")),
        "processed": indexed_progress,
        "indexed": indexed_progress,
        "total": _scan_total,
        "remaining": remaining,
        "percent": percent,
    }


def _blank_scan_phase_state() -> dict[str, Any]:
    return {
        "phase": "",
        "message": "",
        "found": 0,
        "checked": 0,
        "total": 0,
        "new_found": 0,
    }


def _blank_scan_progress() -> dict[str, Any]:
    return {
        "phase": "",
        "processed": 0,
        "indexed": 0,
        "total": 0,
        "remaining": 0,
        "percent": 0.0,
    }


def _reset_scan_runtime_state() -> None:
    global _is_scanning, _scan_indexed, _scan_total, _scan_percent, _scan_start_count, _scan_phase_state

    _is_scanning = False
    _scan_indexed = 0
    _scan_total = 0
    _scan_percent = 0
    _scan_start_count = 0
    _scan_phase_state = _blank_scan_phase_state()


def _status_scan_snapshot(
    summary: dict[str, int],
    pending_files: int,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """
    Keep API scan state aligned with the live library.

    After resets or backend restarts, stale in-memory progress can outlive the
    real library contents. The UI should never see "scan complete" if the DB is
    empty or a retry backlog still exists.
    """
    if (
        not _is_scanning
        and summary["unique_memories"] == 0
        and summary["files_watched"] == 0
        and pending_files == 0
    ):
        _reset_scan_runtime_state()
        return _blank_scan_phase_state(), _blank_scan_progress(), False

    progress = _current_scan_progress(summary["unique_memories"])
    phase = dict(_scan_phase_state)

    if not _is_scanning and pending_files > 0:
        phase = {
            **phase,
            "phase": str(phase.get("phase") or "background"),
            "message": str(
                phase.get("message")
                or f"search is ready while {pending_files} files finish in background"
            ),
        }
        progress = {
            **progress,
            "phase": str(progress.get("phase") or phase["phase"]),
        }

    return phase, progress, _is_scanning


def _background_status_snapshot(
    scan_phase: dict[str, Any],
    progress: dict[str, Any],
    pending_files: int,
    is_scanning: bool,
) -> dict[str, Any]:
    phase_name = str(scan_phase.get("phase") or "")
    message = str(scan_phase.get("message") or "")
    message_lower = message.casefold()
    background_task_count = watcher.get_background_scan_task_count()
    background_phase_active = (
        phase_name == "background"
        or (message_lower.startswith("continuing ") and "background" in message_lower)
    )
    background_running = background_task_count > 0 or background_phase_active

    if background_running:
        pending = max(
            int(progress.get("remaining") or 0),
            pending_files if not is_scanning else 0,
        )
        total = max(int(progress.get("total") or 0), pending)
        completed = min(total, int(progress.get("processed") or 0))
        return {
            "phase": phase_name or "background",
            "state": "running" if is_scanning else "queued",
            "pending": pending,
            "active": max(background_task_count, 1 if is_scanning else 0),
            "completed": completed,
            "total": total,
            "failed": 0,
            "message": message or f"indexing {pending} files in the background",
        }

    if pending_files > 0:
        return {
            "phase": "retry",
            "state": "retrying",
            "pending": pending_files,
            "active": 0,
            "completed": 0,
            "total": pending_files,
            "failed": 0,
            "message": f"retrying {pending_files} files in the background",
        }

    return {
        "phase": phase_name if phase_name == "complete" else "",
        "state": "idle",
        "pending": 0,
        "active": 0,
        "completed": 0,
        "total": 0,
        "failed": 0,
        "message": "",
    }


def _user_settings_path(user_id: str) -> Path:
    settings_dir = config.DATA_DIR / "user_settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    safe_user_id = str(user_id).replace("/", "_")
    return settings_dir / f"{safe_user_id}.json"


def _load_user_settings(user_id: str) -> dict[str, Any]:
    settings_path = _user_settings_path(user_id)
    if not settings_path.exists():
        return {"onboarding_completed": False}

    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Unable to read user settings for %s", user_id, exc_info=True)
        return {"onboarding_completed": False}


def _save_user_settings(user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    settings_path = _user_settings_path(user_id)
    existing = _load_user_settings(user_id)
    existing.update(updates)
    settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return existing


async def _broadcast_event(event: dict[str, Any]) -> None:
    global _startup_health_state

    if event.get("type") == "startup_health" and event.get("status") == "degraded":
        _startup_health_state = dict(event)

    dead: set[WebSocket] = set()
    for websocket in list(_websocket_clients):
        try:
            await websocket.send_json(event)
        except Exception:
            dead.add(websocket)
    _websocket_clients.difference_update(dead)


def _delete_sqlite_family(path: Path) -> None:
    candidates = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
    for candidate in candidates:
        try:
            candidate.unlink(missing_ok=True)
        except Exception:
            logger.warning("Unable to delete %s during startup repair", candidate, exc_info=True)


def _reset_database_files() -> None:
    _delete_sqlite_family(config.DB_PATH)


def _reset_text_index_files() -> None:
    try:
        config.TEXT_FAISS_PATH.unlink(missing_ok=True)
    except Exception:
        logger.warning("Unable to delete text index during reset", exc_info=True)


def _is_retryable_db_startup_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError):
        return any(marker in message for marker in database.RETRYABLE_DB_ERROR_MARKERS)
    return any(marker in message for marker in database.RETRYABLE_DB_ERROR_MARKERS)


def _clear_active_watcher() -> None:
    global _watcher

    if _watcher is None:
        return

    for directory in list(_watcher.watched_dirs):
        _watcher.remove_directory(directory)
    _watcher.watched_dirs = []


def _clear_library_state() -> None:
    global _is_scanning, _scan_indexed, _scan_total, _scan_percent, _scan_start_count, _scan_phase_state

    _clear_active_watcher()
    invalidate_discovery_cache()
    _reset_database_files()
    _reset_text_index_files()
    database.init_db()
    embedder.get_text_index().reset()

    _is_scanning = False
    _scan_indexed = 0
    _scan_total = 0
    _scan_percent = 0
    _scan_start_count = 0
    _scan_phase_state = {
        "phase": "",
        "message": "",
        "found": 0,
        "checked": 0,
        "total": 0,
        "new_found": 0,
    }


def _get_text_embedding_count(conn: Any) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM embeddings
        WHERE embedding_type IN ('dense', 'summary')
          AND faiss_id IS NOT NULL
        """
    ).fetchone()
    return int(row["count"]) if row and row["count"] is not None else 0


def _read_faiss_vector_count() -> int:
    import faiss

    index = faiss.read_index(str(config.TEXT_FAISS_PATH))
    return int(index.ntotal)


def _build_reindex_text(item: dict[str, Any]) -> tuple[str, str | None]:
    summary = str(item.get("summary") or "").strip()
    extracted_text = str(item.get("extracted_text") or "").strip()
    title = str(item.get("title") or item.get("filename") or "").strip()

    seed_text = summary or extracted_text or title
    extra_text = extracted_text or None
    return seed_text, extra_text


def _rebuild_text_index_from_items_sync() -> int:
    conn = database.get_connection()
    rebuilt = 0

    try:
        rows = conn.execute(
            """
            SELECT id, title, filename, summary, extracted_text, handler_type
            FROM items
            WHERE COALESCE(is_excluded, 0) = 0
            ORDER BY first_seen_at, filename
            """
        ).fetchall()

        conn.execute(
            "DELETE FROM embeddings WHERE embedding_type IN ('dense', 'summary', 'sparse_json', 'visual')"
        )
        conn.commit()

        text_index = embedder.get_text_index()
        text_index.reset()

        for row in rows:
            item = dict(row)
            seed_text, extra_text = _build_reindex_text(item)
            if not seed_text:
                continue

            chunked = bool(extra_text and len(extra_text) > pipeline.CHUNKING_TEXT_LENGTH)
            result = embedder.embed_and_index_text(
                str(item["id"]),
                seed_text,
                conn,
                chunked=chunked,
                extra_text=extra_text if chunked else None,
                source_label=str(item.get("filename") or item.get("id") or ""),
                summary_text=str(item.get("summary") or ""),
            )

            if str(item.get("handler_type") or "") == "visual":
                now = datetime.now(timezone.utc).isoformat()
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
                    (
                        str(item["id"]),
                        int(result["faiss_id"]),
                        config.EMBEDDING_MODEL,
                        now,
                    ),
                )
                conn.commit()

            rebuilt += 1

        embedder.save_text_index_if_loaded()
        return rebuilt
    finally:
        conn.close()


def _reset_scan_state(start_count: int = 0) -> None:
    global _is_scanning, _scan_indexed, _scan_total, _scan_percent, _scan_start_count, _scan_phase_state

    _is_scanning = True
    _scan_indexed = 0
    _scan_total = 0
    _scan_percent = 0
    _scan_start_count = start_count
    _scan_phase_state = {
        "phase": "",
        "message": "",
        "found": 0,
        "checked": 0,
        "total": 0,
        "new_found": 0,
    }


def _start_scan_task(paths: list[Path] | None = None, *, start_count: int | None = None) -> asyncio.Task[Any]:
    _reset_scan_state(_get_item_count() if start_count is None else start_count)
    coroutine = _run_scan_for_paths(paths) if paths else _run_scan()
    return _track_background_task(asyncio.create_task(coroutine))


async def _run_faiss_rebuild() -> None:
    try:
        await pipeline.index_events.put(
            {
                "type": "scan_phase",
                "phase": "rebuilding",
                "message": "repairing search index from stored memories...",
                "found": 0,
                "checked": 0,
                "total": 0,
                "new_found": 0,
            }
        )
        rebuilt = await asyncio.to_thread(_rebuild_text_index_from_items_sync)
        await pipeline.index_events.put(
            {
                "type": "scan_complete",
                "indexed": rebuilt,
                "processed": rebuilt,
                "total": rebuilt,
                "percent": 100,
                "message": f"rebuilt search index for {rebuilt} memories",
                "new_found": rebuilt,
            }
        )
        pending_files = _get_pending_watched_file_count()
        watched_paths = _get_watched_folder_paths()
        if pending_files > 0 and watched_paths:
            logger.info(
                "Resuming library scan after FAISS repair (%s files pending)",
                pending_files,
            )
            _start_scan_task(watched_paths, start_count=_get_item_count())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Startup FAISS rebuild failed")
        await pipeline.index_events.put(
            {
                "type": "scan_complete",
                "indexed": 0,
                "processed": 0,
                "total": 0,
                "percent": 0,
                "message": "search index repair failed",
                "new_found": 0,
            }
        )


def _start_faiss_rebuild_task(start_count: int) -> asyncio.Task[Any]:
    _reset_scan_state(start_count)
    return _track_background_task(asyncio.create_task(_run_faiss_rebuild()))


async def startup_health_check() -> dict[str, Any]:
    global _startup_health_state

    issues: list[str] = []
    actions: list[str] = []
    count = 0
    watched_paths: list[Path] = []
    faiss_ok = False
    text_embedding_count = 0
    pending_files = 0
    cognitive_counts = {
        "activation_events": 0,
        "entity_edges": 0,
        "frame_instances": 0,
    }
    cognitive_ready = False
    reconcile_result = {
        "total": 0,
        "still_pending": 0,
        "marked_missing": 0,
        "marked_failed": 0,
        "too_old": 0,
    }

    try:
        database.init_db()
        conn = database.get_connection()
        try:
            database.fix_existing_timestamps(conn)
            reconcile_result = database.reconcile_pending_files(conn)
            count = database.get_item_count(conn)
            text_embedding_count = _get_text_embedding_count(conn)
            pending_files = database.get_pending_watched_file_count(conn)
            watched = database.get_watched_folders(conn)
        finally:
            conn.close()
    except Exception as exc:
        issues.append(f"DB error: {exc}")
        if _is_retryable_db_startup_error(exc):
            actions.append("deferred_db_repair")
            watched = []
        else:
            _reset_database_files()
            database.init_db()
            actions.append("recreated_db")
            count = 0
            text_embedding_count = 0
            conn = database.get_connection()
            try:
                reconcile_result = database.reconcile_pending_files(conn)
                watched = database.get_watched_folders(conn)
            finally:
                conn.close()

    if reconcile_result["total"] > 0:
        actions.append("reconciled_pending_files")

    watched_paths = [Path(str(folder["path"])).expanduser() for folder in watched]
    if not _auto_index_on_start():
        watched_paths = []
        if watched:
            actions.append("startup_scan_disabled")

    if config.TEXT_FAISS_PATH.exists():
        try:
            faiss_count = await asyncio.to_thread(_read_faiss_vector_count)
            baseline = max(text_embedding_count, 1)
            mismatch_ratio = abs(faiss_count - text_embedding_count) / baseline
            if text_embedding_count == 0 and faiss_count == 0:
                faiss_ok = True
            elif mismatch_ratio > 0.05:
                issues.append(
                    f"FAISS mismatch: {faiss_count} vectors vs {text_embedding_count} text embeddings"
                )
                try:
                    config.TEXT_FAISS_PATH.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Unable to delete mismatched FAISS index", exc_info=True)
                actions.append("scheduled_faiss_rebuild")
            else:
                faiss_ok = True
        except Exception as exc:
            issues.append(f"FAISS corrupt: {exc}")
            try:
                config.TEXT_FAISS_PATH.unlink(missing_ok=True)
            except Exception:
                logger.warning("Unable to delete corrupt FAISS index", exc_info=True)
            actions.append("deleted_corrupt_faiss")
    elif text_embedding_count > 0:
        issues.append("FAISS missing but text embeddings exist")
        actions.append("scheduled_faiss_rebuild")
    else:
        faiss_ok = True

    missing_folders = [str(path) for path in watched_paths if not path.exists()]
    if missing_folders:
        issues.append(f"Watched folders missing: {missing_folders}")
        actions.append("flagged_missing_folders")

    try:
        cognitive_store.init_db()
        cognitive_counts = cognitive_store.get_store().get_counts()
        cognitive_ready = True
    except Exception as exc:
        issues.append(f"Cognitive layer error: {exc}")

    if "scheduled_faiss_rebuild" in actions or "deleted_corrupt_faiss" in actions:
        _start_faiss_rebuild_task(count)
        actions.append("started_faiss_rebuild")
    elif watched_paths and (count == 0 or pending_files > 0):
        _start_scan_task(watched_paths, start_count=count)
        actions.append("started_scan")

    event = {
        "type": "startup_health",
        "status": "degraded" if issues else "ok",
        "issues": issues,
        "actions": actions,
        "item_count": count,
    }

    _startup_health_state = dict(event) if issues else None
    await _broadcast_event(event)

    if issues:
        logger.warning("[Startup] Health issues found: %s", issues)
        logger.warning("[Startup] Actions taken: %s", actions)

    return {
        "status": "degraded" if issues else "ok",
        "issues": issues,
        "actions": actions,
        "pending_reconcile": reconcile_result,
        "cognitive_ready": cognitive_ready,
        "cognitive_counts": cognitive_counts,
    }


def _item_exists_on_disk(item: dict[str, Any]) -> bool:
    if str(item.get("source_type") or "").strip().lower() != "local":
        return True
    original_path = str(item.get("original_path") or "").strip()
    return bool(original_path) and Path(original_path).exists()


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _reject_control_chars(value: str, field_name: str) -> str:
    if any(char in value for char in ("\0", "\r", "\n")):
        raise ValueError(f"{field_name} contains invalid characters")
    return value


class SearchRequest(StrictRequestModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    explain: bool = False
    case_id: str | None = Field(default=None, max_length=160)

    @field_validator("query", "case_id")
    @classmethod
    def validate_query(cls, value: str | None) -> str | None:
        return _reject_control_chars(value, "query") if value is not None else value


class CaseCreateRequest(StrictRequestModel):
    name: str = Field(min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=12000)

    @field_validator("name", "description", "notes")
    @classmethod
    def validate_case_text(cls, value: str | None) -> str | None:
        return _reject_control_chars(value, "case text") if value is not None else value


class CasePatchRequest(StrictRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=4000)
    status: str | None = Field(default=None, min_length=1, max_length=80)
    notes: str | None = Field(default=None, max_length=12000)

    @field_validator("name", "description", "status", "notes")
    @classmethod
    def validate_case_patch_text(cls, value: str | None) -> str | None:
        return _reject_control_chars(value, "case text") if value is not None else value

    @model_validator(mode="after")
    def validate_non_nullable_fields(self) -> "CasePatchRequest":
        if self.name is None and "name" in self.model_fields_set:
            raise ValueError("name cannot be null")
        if self.status is None and "status" in self.model_fields_set:
            raise ValueError("status cannot be null")
        return self


class EvidenceImportRequest(StrictRequestModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    hash_algorithm: Literal["sha256"] = "sha256"

    @field_validator("paths")
    @classmethod
    def validate_evidence_paths(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw_path in value:
            path = _reject_control_chars(str(raw_path).strip(), "path")
            if not path:
                continue
            if len(path) > 4096:
                raise ValueError("path is too long")
            cleaned.append(path)
        if not cleaned:
            raise ValueError("At least one path is required")
        return cleaned


class ConversationTurn(StrictRequestModel):
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=4000)


class AskRequest(StrictRequestModel):
    """Request body for the /ask conversational endpoint.

    `limit` controls how many retrieval results are fed to the LLM as sources;
    we overfetch 3x under the hood so the reranker has room to pick the best
    citations. `session_id` is optional — if present, the implicit search
    event we log gets tied to the caller's UI session so the Phase 5 reranker
    can learn from which questions lead to which clicks.
    """

    question: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=6, ge=1, le=20)
    session_id: str | None = Field(default=None, max_length=120)
    case_id: str | None = Field(default=None, max_length=160)
    allow_model: bool = True
    conversation_history: list[ConversationTurn] | None = Field(default=None, max_length=10)

    @field_validator("question", "session_id", "case_id")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return _reject_control_chars(value, "text") if value is not None else value


class ForensicReportRequest(StrictRequestModel):
    question: str | None = Field(default=None, max_length=2000)
    prompt: str | None = Field(default=None, max_length=2000)
    allow_model: bool = False
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("question", "prompt")
    @classmethod
    def validate_report_text(cls, value: str | None) -> str | None:
        return _reject_control_chars(value, "report text") if value is not None else value

    @model_validator(mode="after")
    def require_question_or_prompt(self) -> "ForensicReportRequest":
        if not (self.question or self.prompt):
            raise ValueError("question or prompt is required")
        return self


class FolderRequest(StrictRequestModel):
    path: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=120)
    start_scan: bool = True

    @field_validator("path", "label")
    @classmethod
    def validate_path_text(cls, value: str | None) -> str | None:
        return _reject_control_chars(value, "path") if value is not None else value


class FolderEstimateRequest(StrictRequestModel):
    path: str | None = Field(default=None, max_length=4096)
    folders: list[str] | None = Field(default=None, max_length=20)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return _reject_control_chars(value, "path") if value is not None else value

    @field_validator("folders")
    @classmethod
    def validate_folders(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = []
        for folder in value:
            path = _reject_control_chars(str(folder).strip(), "folders")
            if not path:
                continue
            if len(path) > 4096:
                raise ValueError("folder path is too long")
            cleaned.append(path)
        return cleaned

    @model_validator(mode="after")
    def require_path_or_folders(self) -> "FolderEstimateRequest":
        if not self.path and not self.folders:
            raise ValueError("path or folders is required")
        return self


class CognitiveBootstrapRequest(StrictRequestModel):
    reset: bool = False
    dry_run: bool = False


class CognitiveAccessRequest(StrictRequestModel):
    event_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    metadata: dict[str, Any] | None = None
    duration_seconds: int | None = Field(default=None, ge=0, le=24 * 60 * 60)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        if len(value) > 30:
            raise ValueError("metadata has too many fields")
        cleaned: dict[str, Any] = {}
        for key, raw in value.items():
            clean_key = _reject_control_chars(str(key).strip(), "metadata key")
            if not clean_key or len(clean_key) > 80:
                raise ValueError("metadata key is invalid")
            if isinstance(raw, (str, int, float, bool)) or raw is None:
                clean_value = raw
                if isinstance(clean_value, str):
                    clean_value = _reject_control_chars(clean_value[:500], "metadata value")
                cleaned[clean_key] = clean_value
            else:
                raise ValueError("metadata values must be scalar")
        return cleaned


class GraphBootstrapRequest(StrictRequestModel):
    reset: bool = False
    dry_run: bool = False


class GmailSyncRequest(StrictRequestModel):
    full_sync: bool = False
    historical_days: int | None = Field(default=None, ge=1, le=3650)
    require_attachments: bool = False
    max_messages: int | None = Field(default=None, ge=1, le=10000)


class GmailCleanupRequest(StrictRequestModel):
    reset_history_cursor: bool = False


class LlmConfigureRequest(StrictRequestModel):
    gemini_vision_enabled: bool | None = None
    excluded_dirs: list[str] | None = Field(default=None, max_length=50)

    @field_validator("excluded_dirs")
    @classmethod
    def validate_excluded_dirs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = []
        for raw_path in value:
            path = _reject_control_chars(str(raw_path).strip(), "excluded_dirs")
            if not path:
                continue
            if len(path) > 4096:
                raise ValueError("excluded path is too long")
            cleaned.append(path)
        return cleaned


class UserSettingsUpdate(StrictRequestModel):
    onboarding_completed: bool | None = None
    aliases: dict[str, str] | None = Field(default=None, max_length=100)
    accent_color: Literal["gold", "sage", "blue", "rose", "violet"] | None = None

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return value
        cleaned: dict[str, str] = {}
        for key, raw in value.items():
            alias = _reject_control_chars(str(key).strip(), "alias").lower()
            expansion = _reject_control_chars(str(raw).strip(), "alias expansion")
            if not alias or len(alias) > 80:
                raise ValueError("alias name is invalid")
            if not expansion or len(expansion) > 500:
                raise ValueError("alias expansion is invalid")
            cleaned[alias] = expansion
        return cleaned


class AdminConfigRequest(StrictRequestModel):
    model: Literal[
        "gemini-3.8-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash-8b",
    ] | None = None
    gemini_key: str | None = Field(default=None, min_length=24, max_length=128)

    @field_validator("gemini_key")
    @classmethod
    def validate_gemini_key(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = _reject_control_chars(value.strip(), "gemini_key")
        if not API_KEY_PATTERN.match(value):
            raise ValueError("gemini_key must be a Gemini API key")
        return value


ESTIMATE_DOC_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".md",
    ".xlsx",
    ".xls",
    ".pptx",
    ".csv",
    ".rtf",
    ".pages",
    ".numbers",
    ".key",
}
ESTIMATE_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".tiff",
    ".bmp",
    ".svg",
}
ESTIMATE_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv"}
ESTIMATE_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
ESTIMATE_LIMIT = 20_000


def _classify_estimate_extension(extension: str) -> str:
    if extension in ESTIMATE_DOC_EXTENSIONS:
        return "documents"
    if extension in ESTIMATE_IMAGE_EXTENSIONS:
        return "images"
    if extension in ESTIMATE_VIDEO_EXTENSIONS:
        return "videos"
    if extension in ESTIMATE_AUDIO_EXTENSIONS:
        return "audio"
    return "other"


def _probe_folder_access(folder_path: Path) -> dict[str, Any]:
    try:
        with os.scandir(folder_path) as iterator:
            next(iterator, None)
        return {
            "accessible": True,
            "permission_required": False,
            "message": "",
        }
    except PermissionError:
        return {
            "accessible": False,
            "permission_required": True,
            "message": (
                "Grant Recurdo access to this folder in the macOS prompt, "
                "then try again."
            ),
        }
    except OSError as exc:
        return {
            "accessible": False,
            "permission_required": False,
            "message": f"Unable to access this folder: {exc}",
        }


def _estimate_folder_contents(raw_path: str) -> dict[str, Any]:
    folder_path = Path(raw_path).expanduser()
    if not folder_path.exists() or not folder_path.is_dir():
        return {
            "exists": False,
            "path": str(folder_path),
            "total": 0,
            "by_type": {},
            "capped": False,
            "estimated_minutes": 0,
            "accessible": False,
            "permission_required": False,
            "error_message": "",
        }

    folder_path = folder_path.resolve()
    access_probe = _probe_folder_access(folder_path)
    if not access_probe["accessible"]:
        return {
            "exists": True,
            "path": str(folder_path),
            "total": 0,
            "by_type": {},
            "capped": False,
            "estimated_minutes": 0,
            "accessible": False,
            "permission_required": bool(access_probe["permission_required"]),
            "error_message": str(access_probe["message"]),
        }

    counts = {
        "documents": 0,
        "images": 0,
        "videos": 0,
        "audio": 0,
        "other": 0,
    }
    visible_total = 0
    candidate_total = 0
    foreground_total = 0
    deferred_background = 0
    skipped_too_old = 0
    skipped_too_large = 0
    skipped_excluded = 0
    hidden_items = 0
    already_accounted_for = 0
    capped = False
    conn = database.get_connection()
    try:
        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [
                name
                for name in dirs
                if (
                    not name.startswith(".")
                    and name not in watcher.SCAN_SKIP_PARTS
                    and not any(name.lower().endswith(suffix) for suffix in watcher.SKIP_DIR_SUFFIXES)
                )
            ]

            root_path = Path(root)

            for filename in files:
                if filename.startswith("."):
                    hidden_items += 1
                    continue

                file_path = root_path / filename
                relative_parts = file_path.relative_to(folder_path).parts
                if any(part.startswith(".") for part in relative_parts):
                    hidden_items += 1
                    continue
                if any(part in watcher.SCAN_SKIP_PARTS for part in relative_parts):
                    continue
                if any(
                    part.lower().endswith(suffix)
                    for part in relative_parts
                    for suffix in watcher.SKIP_DIR_SUFFIXES
                ):
                    continue

                visible_total += 1
                reason = watcher._scan_eligibility_reason(file_path, watcher.SCAN_SKIP_PARTS)
                if reason == "candidate":
                    candidate_total += 1
                    if not watcher._is_new_or_changed_preview(conn, file_path):
                        already_accounted_for += 1
                        continue
                    if watcher._is_too_old_for_initial_scan(file_path):
                        skipped_too_old += 1
                    elif watcher._should_defer_initial_background(file_path):
                        deferred_background += 1
                    else:
                        foreground_total += 1
                        counts[_classify_estimate_extension(file_path.suffix.lower())] += 1
                elif reason == "too_large":
                    skipped_too_large += 1
                else:
                    skipped_excluded += 1

                if visible_total >= ESTIMATE_LIMIT:
                    capped = True
                    break

            if capped:
                break
    except PermissionError:
        logger.debug("Estimate skipped unreadable folder %s", folder_path, exc_info=True)
    finally:
        conn.close()

    unsupported_hidden = skipped_excluded + hidden_items
    buckets = {
        "already_in_library": already_accounted_for,
        "will_index": foreground_total,
        "deferred_large": deferred_background,
        "skipped_old": skipped_too_old,
        "unsupported_hidden": unsupported_hidden,
    }
    skipped_breakdown = {
        "old": skipped_too_old,
        "unsupported_hidden": unsupported_hidden,
        "too_large": skipped_too_large,
    }

    return {
        "exists": True,
        "path": str(folder_path),
        "paths": [str(folder_path)],
        "total": foreground_total,
        "foreground_total": foreground_total,
        "candidate_total": candidate_total,
        "visible_total": visible_total,
        "total_candidate_files": visible_total + hidden_items,
        "deferred_background": deferred_background,
        "skipped_too_old": skipped_too_old,
        "skipped_too_large": skipped_too_large,
        "skipped_excluded": skipped_excluded,
        "already_accounted_for": already_accounted_for,
        "hidden_items": hidden_items,
        "buckets": buckets,
        "skipped_breakdown": skipped_breakdown,
        "searchable_after_scan": already_accounted_for + foreground_total,
        "skipped_total": skipped_too_old + unsupported_hidden + skipped_too_large,
        "by_type": counts,
        "capped": capped,
        "estimated_minutes": max(1, round(foreground_total / 500)) if foreground_total else 1,
        "accessible": True,
        "permission_required": False,
        "error_message": "",
    }


def _combine_estimate_results(estimates: list[dict[str, Any]]) -> dict[str, Any]:
    combined = {
        "exists": any(estimate.get("exists") for estimate in estimates),
        "paths": [str(estimate.get("path") or "") for estimate in estimates if estimate.get("path")],
        "path": "",
        "total": 0,
        "foreground_total": 0,
        "candidate_total": 0,
        "visible_total": 0,
        "total_candidate_files": 0,
        "deferred_background": 0,
        "skipped_too_old": 0,
        "skipped_too_large": 0,
        "skipped_excluded": 0,
        "already_accounted_for": 0,
        "hidden_items": 0,
        "buckets": {
            "already_in_library": 0,
            "will_index": 0,
            "deferred_large": 0,
            "skipped_old": 0,
            "unsupported_hidden": 0,
        },
        "skipped_breakdown": {
            "old": 0,
            "unsupported_hidden": 0,
            "too_large": 0,
        },
        "searchable_after_scan": 0,
        "skipped_total": 0,
        "by_type": {
            "documents": 0,
            "images": 0,
            "videos": 0,
            "audio": 0,
            "other": 0,
        },
        "capped": False,
        "estimated_minutes": 0,
        "accessible": all(bool(estimate.get("accessible", True)) for estimate in estimates) if estimates else False,
        "permission_required": any(bool(estimate.get("permission_required")) for estimate in estimates),
        "error_message": "",
        "folders": estimates,
    }

    error_messages = [str(estimate.get("error_message") or "").strip() for estimate in estimates if estimate.get("error_message")]
    if error_messages:
        combined["error_message"] = " · ".join(error_messages[:3])

    for estimate in estimates:
        combined["capped"] = combined["capped"] or bool(estimate.get("capped"))
        for key in (
            "total",
            "foreground_total",
            "candidate_total",
            "visible_total",
            "total_candidate_files",
            "deferred_background",
            "skipped_too_old",
            "skipped_too_large",
            "skipped_excluded",
            "already_accounted_for",
            "hidden_items",
            "searchable_after_scan",
            "skipped_total",
        ):
            combined[key] += int(estimate.get(key) or 0)
        for key in combined["by_type"]:
            combined["by_type"][key] += int((estimate.get("by_type") or {}).get(key) or 0)
        for key in combined["buckets"]:
            combined["buckets"][key] += int((estimate.get("buckets") or {}).get(key) or 0)
        for key in combined["skipped_breakdown"]:
            combined["skipped_breakdown"][key] += int((estimate.get("skipped_breakdown") or {}).get(key) or 0)

    combined["estimated_minutes"] = (
        max(1, round(combined["total"] / 500)) if combined["total"] else 1
    )
    return combined


def _case_or_404(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    case = database.get_case(conn, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


def _case_evidence_manifest_row(row: dict[str, Any]) -> dict[str, Any]:
    source_path = str(row.get("source_path") or "")
    return {
        "case_id": row.get("case_id"),
        "item_id": row.get("item_id"),
        "filename": row.get("filename") or Path(source_path).name,
        "path": source_path,
        "sha256": row.get("sha256"),
        "size_bytes": int(row.get("size_bytes") or 0),
        "mtime_ns": int(row.get("mtime_ns") or 0),
        "imported_at": row.get("imported_at"),
        "acquisition_status": row.get("acquisition_status"),
        "indexed": {
            "status": row.get("indexed_status"),
            "filename": row.get("filename"),
            "title": row.get("title"),
            "file_type": row.get("file_type"),
            "handler_type": row.get("handler_type"),
            "source_type": row.get("source_type"),
            "original_path": row.get("original_path"),
            "created_at": row.get("item_created_at"),
            "modified_at": row.get("item_modified_at"),
            "first_seen_at": row.get("item_first_seen_at"),
            "file_mtime": row.get("indexed_file_mtime"),
            "file_size_bytes": row.get("indexed_file_size_bytes"),
            "last_seen": row.get("indexed_last_seen"),
            "watched_file_size": row.get("indexed_watched_file_size"),
            "is_excluded": bool(row.get("is_excluded")) if row.get("is_excluded") is not None else None,
        },
    }


def _json_safe_custody_event(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    try:
        payload["details"] = json.loads(str(payload.get("details_json") or "{}"))
    except (TypeError, ValueError):
        payload["details"] = {}
    return payload


def _hash_and_stat_source(path: Path, algorithm: str) -> dict[str, Any]:
    stat_result = path.stat()
    digest = custody.hash_file_streaming(path, algorithm=algorithm)
    # Capture the metadata after the read. The digest itself is always computed
    # from the source bytes; these values describe the source observed by import.
    final_stat = path.stat()
    return {
        "sha256": digest,
        "size_bytes": int(final_stat.st_size),
        "mtime_ns": int(final_stat.st_mtime_ns),
        "initial_size_bytes": int(stat_result.st_size),
        "initial_mtime_ns": int(stat_result.st_mtime_ns),
    }


async def _import_case_evidence(
    *,
    case_id: str,
    paths: list[str],
    actor: str,
    hash_algorithm: str,
) -> dict[str, Any]:
    discovered = custody.discover_supported_files(paths)
    requested_paths = [Path(raw_path).expanduser().resolve(strict=False) for raw_path in paths]
    requested_existing_dirs = [
        path for path in requested_paths if path.exists() and path.is_dir()
    ]

    conn = database.get_connection()
    try:
        _case_or_404(conn, case_id)
        for directory in requested_existing_dirs:
            database.add_watched_folder(conn, directory, directory.name or str(directory))
    finally:
        conn.close()

    for directory in requested_existing_dirs:
        if _watcher is not None:
            _watcher.add_directory(directory)

    files_by_path = {str(path): path for path in discovered}
    results: list[dict[str, Any]] = []
    for raw_path in paths:
        candidate = Path(raw_path).expanduser().resolve(strict=False)
        if candidate.is_file() and str(candidate) not in files_by_path:
            results.append({"path": str(candidate), "status": "unsupported"})
        elif not candidate.exists():
            results.append({"path": str(candidate), "status": "missing"})

    for path in discovered:
        file_result: dict[str, Any] = {"path": str(path), "filename": path.name}
        try:
            source = await asyncio.to_thread(_hash_and_stat_source, path, hash_algorithm)
        except FileNotFoundError:
            file_result["status"] = "missing"
            results.append(file_result)
            continue
        except (OSError, PermissionError) as exc:
            file_result.update({"status": "unreadable", "error": str(exc)})
            results.append(file_result)
            continue

        conn = database.get_connection()
        try:
            database.upsert_watched_file(
                conn,
                path,
                last_seen=float(source["mtime_ns"]) / 1_000_000_000,
                file_size=int(source["size_bytes"]),
                status="pending",
            )
        finally:
            conn.close()

        try:
            indexed = await pipeline.index_file_safe(
                path,
                initial_scan=True,
                use_semaphore=True,
            )
        except Exception as exc:
            indexed = {"status": "error", "error": str(exc)}

        item_id = str((indexed or {}).get("item_id") or "").strip()
        index_status = str((indexed or {}).get("status") or "error")
        if not item_id:
            conn = database.get_connection()
            try:
                existing = database.get_item_by_path(conn, path)
                if existing is not None:
                    item_id = str(existing["id"])
            finally:
                conn.close()

        if not item_id:
            file_result.update(
                {
                    "status": "index_error" if index_status == "error" else index_status,
                    "error": (indexed or {}).get("error") or "Indexing did not produce an item",
                }
            )
            results.append(file_result)
            continue

        imported_at = _utc_now_iso()
        evidence = {
            "case_id": case_id,
            "item_id": item_id,
            "source_path": str(path),
            "sha256": source["sha256"],
            "size_bytes": source["size_bytes"],
            "mtime_ns": source["mtime_ns"],
            "imported_at": imported_at,
            "acquisition_status": "acquired",
        }
        conn = database.get_connection()
        try:
            database.upsert_watched_file(
                conn,
                path,
                item_id=item_id,
                last_seen=float(source["mtime_ns"]) / 1_000_000_000,
                file_size=int(source["size_bytes"]),
                status="indexed" if index_status in {"indexed", "already_indexed", "duplicate"} else "pending",
            )
            database.record_case_evidence_acquisition(
                conn,
                evidence,
                actor=actor,
                occurred_at=imported_at,
                details={
                    "source_path": str(path),
                    "sha256": source["sha256"],
                    "size_bytes": source["size_bytes"],
                    "mtime_ns": source["mtime_ns"],
                    "hash_algorithm": hash_algorithm,
                    "index_status": index_status,
                },
            )
        finally:
            conn.close()

        file_result.update(
            {
                "status": index_status,
                "item_id": item_id,
                "sha256": source["sha256"],
                "size_bytes": source["size_bytes"],
                "mtime_ns": source["mtime_ns"],
            }
        )
        results.append(file_result)

    status_counts: dict[str, int] = {}
    for file_result in results:
        file_status = str(file_result.get("status") or "unknown")
        status_counts[file_status] = status_counts.get(file_status, 0) + 1

    imported_count = sum(
        status_counts.get(status, 0)
        for status in ("indexed", "already_indexed", "duplicate")
    )
    skipped_count = sum(
        status_counts.get(status, 0)
        for status in ("missing", "unsupported", "skipped_age")
    )
    failed_count = sum(
        count
        for status, count in status_counts.items()
        if status not in {
            "indexed",
            "already_indexed",
            "duplicate",
            "missing",
            "unsupported",
            "skipped_age",
        }
    )

    return {
        "case_id": case_id,
        "requested": len(paths),
        "discovered": len(discovered),
        "files": results,
        "summary": {
            "imported": imported_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "missing": status_counts.get("missing", 0),
            "unreadable": status_counts.get("unreadable", 0),
            "by_status": status_counts,
        },
    }


# Registered after the FastAPI application is constructed below.
async def api_list_cases(user: dict = Depends(require_access)) -> list[dict[str, Any]]:
    del user
    conn = database.get_connection()
    try:
        return database.list_cases(conn)
    finally:
        conn.close()


async def api_create_case(
    body: CaseCreateRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    conn = database.get_connection()
    try:
        return database.create_case(
            conn,
            name=body.name,
            description=body.description,
            notes=body.notes,
        )
    finally:
        conn.close()


async def api_get_case(
    case_id: str,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    conn = database.get_connection()
    try:
        case = _case_or_404(conn, case_id)
        case["evidence_count"] = len(database.list_case_evidence(conn, case_id))
        return case
    finally:
        conn.close()


async def api_patch_case(
    case_id: str,
    body: CasePatchRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    conn = database.get_connection()
    try:
        _case_or_404(conn, case_id)
        return database.update_case(conn, case_id, body.model_dump(exclude_unset=True)) or {}
    finally:
        conn.close()


async def api_delete_case(
    case_id: str,
    user: dict = Depends(require_access),
) -> dict[str, bool]:
    del user
    conn = database.get_connection()
    try:
        _case_or_404(conn, case_id)
        database.delete_case(conn, case_id)
    finally:
        conn.close()
    return {"success": True}


async def api_import_case_evidence(
    case_id: str,
    body: EvidenceImportRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    actor = str(user.get("id") or "local")
    return await _import_case_evidence(
        case_id=case_id,
        paths=body.paths,
        actor=actor,
        hash_algorithm=body.hash_algorithm,
    )


async def api_case_evidence(
    case_id: str,
    user: dict = Depends(require_access),
) -> list[dict[str, Any]]:
    del user
    conn = database.get_connection()
    try:
        _case_or_404(conn, case_id)
        rows = database.list_case_evidence(conn, case_id)
        return [_case_evidence_manifest_row(row) for row in rows]
    finally:
        conn.close()


async def api_case_timeline(
    case_id: str,
    user: dict = Depends(require_access),
) -> list[dict[str, Any]]:
    del user
    conn = database.get_connection()
    try:
        _case_or_404(conn, case_id)
        events = database.list_custody_events(conn, case_id)
        return [_json_safe_custody_event(event) for event in events]
    finally:
        conn.close()


async def api_case_forensic_timeline(
    case_id: str,
    start_at: str | None = Query(default=None, max_length=80),
    end_at: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=1000, ge=1, le=5000),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    conn = database.get_connection()
    try:
        _case_or_404(conn, case_id)
        evidence_rows = database.list_case_evidence(conn, case_id)
        custody_rows = database.list_custody_events(conn, case_id)
    finally:
        conn.close()

    try:
        return forensic.build_forensic_timeline(
            case_id,
            evidence_rows,
            custody_rows,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def api_case_report(
    case_id: str,
    body: ForensicReportRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    question = str(body.question or body.prompt or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question or prompt is required")

    conn = database.get_connection()
    try:
        case = _case_or_404(conn, case_id)
        evidence_rows = database.list_case_evidence(conn, case_id)
        custody_rows = database.list_custody_events(conn, case_id)
    finally:
        conn.close()

    return await forensic.build_forensic_report(
        case=case,
        question=question,
        evidence_rows=evidence_rows,
        custody_rows=custody_rows,
        allow_model=bool(body.allow_model),
        limit=int(body.limit),
    )


async def api_export_case(
    case_id: str,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    conn = database.get_connection()
    try:
        case = _case_or_404(conn, case_id)
        evidence = [_case_evidence_manifest_row(row) for row in database.list_case_evidence(conn, case_id)]
        events = [_json_safe_custody_event(event) for event in database.list_custody_events(conn, case_id)]
    finally:
        conn.close()

    manifest = {"case": case, "evidence": evidence, "events": events}
    return {
        **manifest,
        "manifest_sha256": hashlib.sha256(
            database.canonical_json(manifest).encode("utf-8")
        ).hexdigest(),
    }


async def api_verify_case(
    case_id: str,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    actor = str(user.get("id") or "local")
    conn = database.get_connection()
    try:
        _case_or_404(conn, case_id)
        evidence_rows = database.list_case_evidence(conn, case_id)
    finally:
        conn.close()

    counts = {"verified": 0, "missing": 0, "changed": 0, "unreadable": 0}
    files: list[dict[str, Any]] = []
    for evidence in evidence_rows:
        path = Path(str(evidence.get("source_path") or ""))
        result: dict[str, Any] = {
            "item_id": evidence.get("item_id"),
            "path": str(path),
            "expected_sha256": evidence.get("sha256"),
            "expected_size_bytes": evidence.get("size_bytes"),
            "expected_mtime_ns": evidence.get("mtime_ns"),
        }
        try:
            current = await asyncio.to_thread(_hash_and_stat_source, path, "sha256")
        except FileNotFoundError:
            status = "missing"
            result["error"] = "source file is missing"
        except (OSError, PermissionError) as exc:
            status = "unreadable"
            result["error"] = str(exc)
        else:
            result.update(
                {
                    "sha256": current["sha256"],
                    "size_bytes": current["size_bytes"],
                    "mtime_ns": current["mtime_ns"],
                }
            )
            status = (
                "verified"
                if current["sha256"] == evidence.get("sha256")
                and current["size_bytes"] == int(evidence.get("size_bytes") or 0)
                and current["mtime_ns"] == int(evidence.get("mtime_ns") or 0)
                else "changed"
            )

        counts[status] += 1
        result["status"] = status
        files.append(result)

        conn = database.get_connection()
        try:
            database.record_case_evidence_verification(
                conn,
                case_id=case_id,
                item_id=str(evidence["item_id"]),
                status=status,
                actor=actor,
                details={"path": str(path), **result},
            )
        finally:
            conn.close()

    return {"case_id": case_id, **counts, "files": files}


def _open_local_file(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    subprocess.Popen(["xdg-open", str(path)])


def _reveal_local_file(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
        return
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path.parent)])


def _is_url_target(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _open_url_target(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
        return
    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
        return
    subprocess.Popen(["xdg-open", url])


def _record_item_event_best_effort(
    item_id: str,
    event_type: str,
    *,
    duration_seconds: int | None = None,
) -> None:
    conn = database.get_connection()
    try:
        database.log_event(conn, item_id, event_type, duration_seconds=duration_seconds)
    except Exception:
        logger.exception("Failed to log %s event for %s", event_type, item_id)
    finally:
        conn.close()


def _log_item_open_best_effort(item_id: str) -> None:
    _record_item_event_best_effort(item_id, "open")
    _record_cognitive_event_best_effort(item_id, "open")


def _record_cognitive_event_best_effort(
    item_id: str,
    event_type: str,
    *,
    event_weight: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        cognitive_store.get_store().record_activation_event(
            item_id,
            event_type,
            event_weight=event_weight,
            metadata=metadata,
        )
    except Exception:
        logger.exception("Failed to log cognitive event %s for %s", event_type, item_id)


async def _run_cognitive_bootstrap(
    *,
    reset: bool,
    dry_run: bool,
    requested_by: str | None = None,
) -> None:
    loop = asyncio.get_running_loop()

    def progress_callback(progress: dict[str, Any]) -> None:
        event = {
            "state": progress.get("state", "running"),
            "phase": progress.get("phase", ""),
            "message": progress.get("message", ""),
            "processed": progress.get("processed", 0),
            "total": progress.get("total", 0),
            "percent": progress.get("percent", 0.0),
            "dry_run": dry_run,
            "reset": reset,
            "requested_by": requested_by,
            "summary": progress.get("summary", {}),
        }
        loop.call_soon_threadsafe(_emit_cognitive_bootstrap_event_sync, event)

    try:
        summary = await asyncio.to_thread(
            cognitive_bootstrap.bootstrap_memory_db,
            config.DB_PATH,
            cognitive_db_path=config.COGNITIVE_DB_PATH,
            dry_run=dry_run,
            reset=reset,
            progress_callback=progress_callback,
        )
        _emit_cognitive_bootstrap_event_sync(
            {
                "state": "completed",
                "phase": "complete",
                "message": (
                    "cognitive dry run complete"
                    if dry_run
                    else "cognitive bootstrap complete"
                ),
                "processed": int(summary.get("items", 0) or 0),
                "total": int(summary.get("items", 0) or 0),
                "percent": 100.0,
                "dry_run": dry_run,
                "reset": reset,
                "requested_by": requested_by,
                "completed_at": _utc_now_iso(),
                "summary": summary,
            }
        )
    except Exception as exc:
        logger.exception("Cognitive bootstrap failed")
        _emit_cognitive_bootstrap_event_sync(
            {
                "state": "error",
                "phase": "error",
                "message": f"cognitive bootstrap failed: {exc}",
                "error": str(exc),
                "dry_run": dry_run,
                "reset": reset,
                "requested_by": requested_by,
                "completed_at": _utc_now_iso(),
            }
        )


def _start_cognitive_bootstrap_task(
    *,
    reset: bool,
    dry_run: bool,
    requested_by: str | None = None,
) -> asyncio.Task[Any]:
    global _cognitive_bootstrap_task

    _emit_cognitive_bootstrap_event_sync(
        {
            "state": "running",
            "phase": "starting",
            "message": "starting cognitive bootstrap",
            "processed": 0,
            "total": 0,
            "percent": 0.0,
            "dry_run": dry_run,
            "reset": reset,
            "requested_by": requested_by,
            "started_at": _utc_now_iso(),
            "summary": {},
        }
    )

    task = _track_background_task(
        asyncio.create_task(
            _run_cognitive_bootstrap(
                reset=reset,
                dry_run=dry_run,
                requested_by=requested_by,
            )
        )
    )
    _cognitive_bootstrap_task = task

    def _cleanup(done_task: asyncio.Task[Any]) -> None:
        global _cognitive_bootstrap_task
        if _cognitive_bootstrap_task is done_task:
            _cognitive_bootstrap_task = None

    task.add_done_callback(_cleanup)
    return task


async def _run_scan() -> None:
    global _is_scanning
    try:
        await watcher.initial_scan()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Initial scan failed")
    finally:
        _is_scanning = False


async def _run_scan_for_paths(paths: list[Path]) -> None:
    global _is_scanning
    try:
        await watcher.initial_scan(paths)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Folder scan failed")
    finally:
        _is_scanning = False


async def _run_pending_retry_loop() -> None:
    while True:
        try:
            await asyncio.sleep(PENDING_RETRY_POLL_SECONDS)

            if _is_scanning:
                continue

            if not _auto_index_on_start():
                continue

            if _get_pending_watched_file_count() <= 0:
                continue

            if not _get_watched_folder_paths():
                continue

            retried = await watcher.retry_pending_files()
            if retried > 0:
                logger.info(
                    "Background pending retry processed %s files (%s still pending)",
                    retried,
                    _get_pending_watched_file_count(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending retry loop failed")


async def broadcast_events() -> None:
    global _is_scanning, _scan_indexed, _scan_total, _scan_percent, _scan_phase_state, _startup_health_state

    try:
        while True:
            event = await pipeline.index_events.get()
            event_type = event.get("type")
            if event_type == "scan_phase":
                _is_scanning = True
                _scan_phase_state = {
                    "phase": str(event.get("phase") or _scan_phase_state.get("phase") or ""),
                    "message": str(event.get("message") or _scan_phase_state.get("message") or ""),
                    "found": int(event.get("found", _scan_phase_state.get("found", 0)) or 0),
                    "checked": int(event.get("checked", _scan_phase_state.get("checked", 0)) or 0),
                    "total": int(event.get("total", _scan_phase_state.get("total", 0)) or 0),
                    "new_found": int(event.get("new_found", _scan_phase_state.get("new_found", 0)) or 0),
                }
            elif event_type == "scan_progress":
                _is_scanning = True
                _scan_indexed = int(event.get("indexed", _scan_indexed))
                _scan_total = int(event.get("total", _scan_total))
                _scan_percent = int(event.get("percent", _scan_percent))
                _scan_phase_state = {
                    "phase": "indexing",
                    "message": str(
                        event.get("message")
                        or _scan_phase_state.get("message")
                        or f"indexing {_scan_total} new memories..."
                    ),
                    "found": int(_scan_phase_state.get("found", 0) or 0),
                    "checked": _scan_indexed,
                    "total": _scan_total,
                    "new_found": int(
                        event.get("new_found", _scan_phase_state.get("new_found", _scan_total))
                        or 0
                    ),
                }
            elif event_type == "startup_health":
                if event.get("status") == "degraded":
                    _startup_health_state = dict(event)
            elif event_type == "scan_complete":
                _is_scanning = False
                _scan_total = int(event.get("total", _scan_total))
                _scan_indexed = int(event.get("indexed", _scan_total))
                _scan_percent = int(
                    event.get("percent", 100 if _scan_total else _scan_percent)
                )
                _scan_phase_state = {
                    "phase": "complete",
                    "message": str(
                        event.get("message")
                        or f"stored {_scan_indexed} memories"
                    ),
                    "found": int(_scan_phase_state.get("found", 0) or 0),
                    "checked": _scan_total,
                    "total": _scan_total,
                    "new_found": int(
                        event.get("new_found", _scan_phase_state.get("new_found", _scan_total))
                        or 0
                    ),
                }
            await _broadcast_event(event)
    except asyncio.CancelledError:
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app

    global _watcher, _is_scanning, _scan_indexed, _scan_total, _scan_percent, _scan_start_count, _scan_phase_state, _startup_health_state

    broadcast_task: asyncio.Task | None = None
    watcher_task: asyncio.Task | None = None
    warmup_task: asyncio.Task | None = None
    pending_retry_task: asyncio.Task | None = None
    keepalive_task: asyncio.Task | None = None

    broadcast_task = asyncio.create_task(broadcast_events())
    pending_retry_task = asyncio.create_task(_run_pending_retry_loop())
    keepalive_task = asyncio.create_task(_run_keepalive_loop())

    health_result = await startup_health_check()
    loop = asyncio.get_running_loop()
    watched_dirs = watcher.load_watched_dirs() if _auto_index_on_start() else []
    if not watched_dirs and not _auto_index_on_start():
        logger.info("Automatic startup indexing disabled; waiting for explicit scan")
    _watcher = watcher.MemoryEngineWatcher(watched_dirs, loop)
    watcher_task = asyncio.create_task(_start_live_watcher(_watcher))
    deferred_visual_jobs = await pipeline.bootstrap_deferred_visual_ocr()
    count = _get_item_count()
    warmup_task = asyncio.create_task(_warm_backend_assets())

    logger.info("Recurdo ready")
    logger.info("Text search: nomic-embed-text-v1.5 (local)")
    logger.info("Image OCR: %s", "tesseract" if HAS_TESSERACT else "unavailable")
    logger.info(
        "Image vision: %s",
        "Gemini Flash"
        if HAS_GEMINI_VISION
        else (
            "disabled (sdk not installed)"
            if config.GEMINI_API_KEY and not config.HAS_GEMINI_SDK
            else "disabled (no API key)"
        ),
    )
    logger.info(
        "Enrichment: %s",
        "Gemini Flash"
        if config.GEMINI_API_KEY and config.HAS_GEMINI_SDK
        else (
            "disabled (sdk not installed)"
            if config.GEMINI_API_KEY and not config.HAS_GEMINI_SDK
            else "disabled (no API key)"
        ),
    )
    logger.info("Indexed items: %s", count)
    if deferred_visual_jobs:
        logger.info("Deferred visual OCR jobs queued: %s", deferred_visual_jobs)
    logger.info("Startup health: %s", health_result["status"])

    try:
        yield
    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await watcher_task

        if _watcher is not None:
            try:
                _watcher.stop()
            except Exception:
                logger.exception("Unable to stop live watcher cleanly")
            _watcher = None

        if broadcast_task is not None:
            broadcast_task.cancel()
            with suppress(asyncio.CancelledError):
                await broadcast_task

        if pending_retry_task is not None:
            pending_retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await pending_retry_task

        if keepalive_task is not None:
            keepalive_task.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive_task

        if warmup_task is not None:
            warmup_task.cancel()
            with suppress(asyncio.CancelledError):
                await warmup_task

        for task in list(_background_tasks):
            task.cancel()
        for task in list(_background_tasks):
            with suppress(asyncio.CancelledError):
                await task
        _background_tasks.clear()

        embedder.save_text_index_if_loaded()

        _is_scanning = False
        _scan_indexed = 0
        _scan_total = 0
        _scan_percent = 0
        _scan_start_count = 0
        _scan_phase_state = {
            "phase": "",
            "message": "",
            "found": 0,
            "checked": 0,
            "total": 0,
            "new_found": 0,
        }
        _startup_health_state = None


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_api_route("/cases", api_list_cases, methods=["GET"])
app.add_api_route("/cases", api_create_case, methods=["POST"])
app.add_api_route("/cases/{case_id}", api_get_case, methods=["GET"])
app.add_api_route("/cases/{case_id}", api_patch_case, methods=["PATCH"])
app.add_api_route("/cases/{case_id}", api_delete_case, methods=["DELETE"])
app.add_api_route(
    "/cases/{case_id}/evidence/import",
    api_import_case_evidence,
    methods=["POST"],
)
app.add_api_route("/cases/{case_id}/evidence", api_case_evidence, methods=["GET"])
app.add_api_route("/cases/{case_id}/timeline", api_case_timeline, methods=["GET"])
app.add_api_route(
    "/cases/{case_id}/forensic-timeline",
    api_case_forensic_timeline,
    methods=["GET"],
)
app.add_api_route("/cases/{case_id}/report", api_case_report, methods=["POST"])
app.add_api_route("/cases/{case_id}/export", api_export_case, methods=["GET"])
app.add_api_route("/cases/{case_id}/verify", api_verify_case, methods=["POST"])


@app.middleware("http")
async def rate_limit_public_api(request: Request, call_next):
    if request.method.upper() == "OPTIONS":
        return await call_next(request)

    (ip_limit, ip_window), user_rule = _limits_for_request(request)
    ip_key = f"ip:{_client_ip(request)}:{request.url.path}"
    allowed, retry_after = _rate_limiter.check(
        ip_key,
        limit=ip_limit,
        window_seconds=ip_window,
    )
    if not allowed:
        return _rate_limit_response(retry_after)

    user_subject = _jwt_subject_from_request(request)
    if user_subject and user_rule is not None:
        user_limit, user_window = user_rule
        user_key = f"user:{user_subject}:{request.url.path}"
        allowed, retry_after = _rate_limiter.check(
            user_key,
            limit=user_limit,
            window_seconds=user_window,
        )
        if not allowed:
            return _rate_limit_response(retry_after)

    return await call_next(request)


# The /health endpoint is polled by the desktop UI every ~2-5s. Each call
# previously fanned out to a DB count, a FAISS search ping, and a separate
# cognitive-db connection, so steady-state idle was continuously hitting
# SQLite + FAISS. A 2-second TTL collapses that churn without making the
# status any more stale than the UI's own refresh cadence.
_HEALTH_CACHE_TTL_SECONDS = 2.0
_health_cache_lock = asyncio.Lock()
_health_cache_payload: dict[str, Any] | None = None
_health_cache_captured_at: float = 0.0


async def _cached_health_payload() -> dict[str, Any]:
    global _health_cache_payload, _health_cache_captured_at
    # Snapshot both globals into locals up front so the fast-path decision is
    # made off a single coherent read. The /health handler only runs in the
    # event-loop thread (the heavy compute is offloaded under the lock via
    # asyncio.to_thread), so there is no true multi-threaded reader of these
    # globals — but pulling them into locals makes that invariant explicit
    # and immune to any future refactor that adds a thread-side reader.
    now = time.time()
    cached_payload = _health_cache_payload
    cached_at = _health_cache_captured_at
    if (
        cached_payload is not None
        and (now - cached_at) < _HEALTH_CACHE_TTL_SECONDS
    ):
        return cached_payload
    async with _health_cache_lock:
        # Re-check under the lock so concurrent pollers share one refresh.
        now = time.time()
        cached_payload = _health_cache_payload
        cached_at = _health_cache_captured_at
        if (
            cached_payload is not None
            and (now - cached_at) < _HEALTH_CACHE_TTL_SECONDS
        ):
            return cached_payload
        payload = await asyncio.to_thread(_compute_health_payload, now)
        _health_cache_payload = payload
        _health_cache_captured_at = now
        return payload


@app.get("/health")
async def get_health() -> dict[str, Any]:
    return await _cached_health_payload()


@app.get("/ready")
async def get_ready() -> dict[str, Any]:
    """Fast readiness probe for the desktop client.

    Important: keep this endpoint lightweight and non-blocking. The UI uses it
    as the backend "is alive" signal during startup, so it must not depend on
    SQLite/FAISS state (those can be rebuilding or locked temporarily).
    """
    current = time.time()
    return {
        "status": "ok",
        "uptime_s": max(0, int(current - _server_started_at)),
    }


@app.get("/auth/status")
async def auth_status(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    access = get_access_info(user["id"], user.get("token"))
    if access.get("verification_unavailable"):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ACCESS_UNAVAILABLE",
                "message": "Unable to verify your access right now.",
            },
        )
    verification_mode = (
        "local-forensics"
        if LOCAL_MODE
        else "offline-cache"
        if access.get("offline_cached")
        else "supabase-rpc"
        if DIRECT_ACCESS_CONFIGURED
        else "remote-access-api"
        if REMOTE_ACCESS_CONFIGURED
        else "local-dev"
    )
    return {
        "authenticated": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
        },
        "access": {
            "has_access": access.get("has_access", False),
            "tier": access.get("tier", "unknown"),
            "days_remaining": access.get("days_remaining", 0),
            "expires_at": access.get("expires_at"),
            "is_blocked": access.get("is_blocked", False),
            "offline_cached": access.get("offline_cached", False),
            "verification_unavailable": access.get("verification_unavailable", False),
            "verification_mode": verification_mode,
        },
    }


@app.get("/user/settings")
async def get_user_settings(user: dict = Depends(require_access)) -> dict[str, Any]:
    return _load_user_settings(str(user["id"]))


@app.post("/user/settings")
async def update_user_settings(
    settings: UserSettingsUpdate,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    return _save_user_settings(str(user["id"]), settings.model_dump(exclude_none=True))


@app.get("/user/profile")
async def get_user_profile(user: dict = Depends(require_access)) -> dict[str, Any]:
    email = str(user.get("email") or "")
    name = (
        str(user.get("user_metadata", {}).get("full_name") or "").strip()
        or str(user.get("name") or "").strip()
        or (email.split("@")[0] if email else "")
    )
    return {
        "id": str(user.get("id") or ""),
        "email": email,
        "name": name,
        "display_name": name,
    }


@app.get("/index/status")
async def get_index_status(user: dict = Depends(require_access)) -> dict[str, Any]:
    summary = _get_count_summary()
    pending_files = _get_pending_watched_file_count()
    scan_phase, progress, is_scanning = _status_scan_snapshot(summary, pending_files)
    background = _background_status_snapshot(scan_phase, progress, pending_files, is_scanning)
    cognitive_status = _get_cognitive_status_snapshot()
    graph_status = _get_graph_status_snapshot()
    llm_status = _get_llm_status_snapshot()
    gmail_status = _get_gmail_status_snapshot()

    return {
        "total_items": summary["unique_memories"],
        "unique_memories": summary["unique_memories"],
        "files_watched": summary["files_watched"],
        "duplicates_skipped": summary["duplicates_skipped"],
        "pending_files": pending_files,
        "queue_size": pipeline.index_events.qsize(),
        "is_scanning": is_scanning,
        "is_fully_indexed": (not is_scanning) and background["pending"] == 0,
        "background": background,
        "scan_phase": scan_phase,
        "scan_progress": progress,
        "cognitive": cognitive_status,
        "graph": graph_status,
        "llm": llm_status,
        "gmail": gmail_status,
    }


@app.post("/index/trigger")
async def trigger_index(user: dict = Depends(require_access)) -> dict[str, str]:
    global _is_scanning, _scan_indexed, _scan_total, _scan_percent, _scan_start_count, _scan_phase_state

    if _is_scanning:
        return {"status": "already_scanning"}

    _is_scanning = True
    _scan_indexed = 0
    _scan_total = 0
    _scan_percent = 0
    _scan_start_count = _get_item_count()
    _scan_phase_state = {
        "phase": "",
        "message": "",
        "found": 0,
        "checked": 0,
        "total": 0,
        "new_found": 0,
    }
    watched_paths = _get_watched_folder_paths()
    if watched_paths:
        asyncio.create_task(_run_scan_for_paths(watched_paths))
    else:
        asyncio.create_task(_run_scan())
    return {"status": "scanning_started"}


@app.get("/cognitive/status")
async def get_cognitive_status(user: dict = Depends(require_access)) -> dict[str, Any]:
    return _get_cognitive_status_snapshot()


@app.get("/graph/status")
async def get_graph_status(user: dict = Depends(require_access)) -> dict[str, Any]:
    del user
    return _get_graph_status_snapshot()


@app.get("/llm/status")
async def get_llm_status(user: dict = Depends(require_access)) -> dict[str, Any]:
    del user
    return _get_llm_status_snapshot()


@app.post("/llm/configure")
async def configure_llm(body: LlmConfigureRequest, user: dict = Depends(require_access)) -> dict[str, Any]:
    del user

    env_path = config.USER_BACKEND_ENV_PATH
    existing_lines: list[str] = env_path.read_text().splitlines() if env_path.exists() else []
    # Build a mutable dict from existing env so we can patch individual keys
    env_map: dict[str, str] = {}
    for ln in existing_lines:
        if "=" in ln and not ln.startswith("#"):
            k, _, v = ln.partition("=")
            env_map[k.strip()] = v.strip()

    payload = body.model_dump(exclude_none=True)

    # --- Gemini vision toggle ---
    if "gemini_vision_enabled" in payload:
        enabled = bool(payload["gemini_vision_enabled"])
        if enabled and not bool(getattr(config, "RECURDO_ENABLE_GEMINI_VISION", False)):
            raise HTTPException(
                status_code=409,
                detail="Photo descriptions are coming soon. This beta build does not enable Gemini vision yet.",
            )
        config.GEMINI_VISION_ENABLED = enabled
        env_map["GEMINI_VISION_ENABLED"] = "true" if enabled else "false"

    # --- Excluded directories ---
    if "excluded_dirs" in payload:
        raw_dirs = payload["excluded_dirs"]
        from pathlib import Path as _Path
        resolved = [str(_Path(d).expanduser().resolve()) for d in raw_dirs if str(d).strip()]
        config.EXCLUDED_DIRS = [_Path(d) for d in resolved]
        env_map["EXCLUDED_DIRS"] = ",".join(resolved)

    # Write back the env file
    env_path.write_text(
        "\n".join(f"{k}={v}" for k, v in env_map.items()) + "\n"
    )

    return _get_llm_status_snapshot()


@app.get("/gmail/status")
async def get_gmail_status(user: dict = Depends(require_access)) -> dict[str, Any]:
    del user
    return _get_gmail_status_snapshot()


@app.post("/gmail/connect")
async def connect_gmail(user: dict = Depends(require_access)) -> dict[str, Any]:
    del user
    snapshot = _get_gmail_status_snapshot()
    if not snapshot.get("configured"):
        raise HTTPException(
            status_code=409,
            detail=f"Gmail client secrets not found at {snapshot.get('client_secrets_path')}",
        )
    if not snapshot.get("oauthlib_available"):
        raise HTTPException(
            status_code=409,
            detail="google-auth-oauthlib is not installed in the active environment",
        )
    status = _start_gmail_oauth_task()
    snapshot = _get_gmail_status_snapshot()
    return {
        "status": "already_running" if status == "already_running" else "oauth_started",
        "gmail": snapshot,
    }


@app.post("/gmail/sync")
async def trigger_gmail_sync(
    body: GmailSyncRequest | None = None,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    gmail_status = _get_gmail_status_snapshot()
    if not gmail_status.get("connected"):
        raise HTTPException(status_code=409, detail="Connect Gmail before starting a sync")

    payload = body or GmailSyncRequest()
    status = _start_gmail_sync_task(
        full_sync=bool(payload.full_sync),
        historical_days=payload.historical_days,
        require_attachments=bool(payload.require_attachments),
        max_messages=payload.max_messages,
    )
    snapshot = _get_gmail_status_snapshot()
    return {
        "status": "already_running" if status == "already_running" else "sync_started",
        "gmail": snapshot,
    }


@app.post("/gmail/stop")
async def stop_gmail_sync(user: dict = Depends(require_access)) -> dict[str, Any]:
    del user
    status = _stop_gmail_sync_task()
    return {
        "status": status,
        "gmail": _get_gmail_status_snapshot(),
    }


@app.post("/gmail/cleanup")
async def cleanup_gmail_imports(
    body: GmailCleanupRequest | None = None,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    if _gmail_sync_task is not None and not _gmail_sync_task.done():
        raise HTTPException(status_code=409, detail="Stop Gmail sync before clearing imported mail")

    payload = body or GmailCleanupRequest()
    summary = await asyncio.to_thread(
        cleanup_gmail_items,
        reset_history_cursor=bool(payload.reset_history_cursor),
    )
    return {
        "status": "cleanup_complete",
        "summary": summary,
        "gmail": _get_gmail_status_snapshot(),
    }


@app.delete("/gmail/connection")
async def disconnect_gmail(user: dict = Depends(require_access)) -> dict[str, Any]:
    del user
    if _gmail_oauth_task is not None and not _gmail_oauth_task.done():
        raise HTTPException(status_code=409, detail="Cannot disconnect while Gmail authentication is running")
    if _gmail_sync_task is not None and not _gmail_sync_task.done():
        raise HTTPException(status_code=409, detail="Cannot disconnect while Gmail sync is running")

    GmailClient().clear_saved_state()
    _gmail_oauth_state.update(_blank_gmail_job_state())
    _gmail_sync_state.update(_blank_gmail_job_state())
    return {"status": "disconnected", "gmail": _get_gmail_status_snapshot()}


@app.get("/context")
async def get_cognitive_context(
    current_item_id: str | None = Query(default=None, max_length=160),
    current_query: str | None = Query(default=None, max_length=500),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    return _get_context_snapshot(
        current_item_id=current_item_id,
        current_query=current_query,
    )


@app.get("/context/briefing")
async def get_context_briefing(
    current_item_id: str | None = Query(default=None, max_length=160),
    current_query: str | None = Query(default=None, max_length=500),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    engine = ProductionEngine(
        cognitive_store.get_store(),
        memory_db_path=config.DB_PATH,
    )
    context = engine.build_context(
        current_item_id=current_item_id,
        current_query=current_query,
    )
    snapshot = _get_context_snapshot(
        current_item_id=current_item_id,
        current_query=current_query,
    )
    current_item = context.get("current_item") or {}
    current_title = (
        current_item.get("title")
        or current_item.get("filename")
        or current_item_id
        or current_query
        or "current context"
    )
    briefing = await synthesize_proactive_briefing(
        str(current_title),
        list(context.get("related_items") or []),
    )
    return {
        **snapshot,
        "related_items": list(context.get("related_items") or []),
        "briefing": briefing,
    }


@app.post("/bootstrap")
async def trigger_cognitive_bootstrap(
    body: CognitiveBootstrapRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    if _is_cognitive_bootstrap_running():
        return {
            "status": "already_running",
            "cognitive": _get_cognitive_status_snapshot(),
        }

    requested_by = str(user.get("email") or user.get("id") or "")
    _start_cognitive_bootstrap_task(
        reset=bool(body.reset),
        dry_run=bool(body.dry_run),
        requested_by=requested_by or None,
    )
    return {
        "status": "started",
        "cognitive": _get_cognitive_status_snapshot(),
    }


@app.post("/graph/bootstrap")
async def trigger_graph_bootstrap(
    body: GraphBootstrapRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    summary = await asyncio.to_thread(
        graph_bootstrap.bootstrap_memory_db,
        config.DB_PATH,
        graph_db_path=config.GRAPH_DB_PATH,
        dry_run=bool(body.dry_run),
        reset=bool(body.reset),
    )
    return {
        "status": "completed",
        "summary": summary,
        "graph": _get_graph_status_snapshot(),
    }


@app.get("/graph/timeline")
async def get_graph_timeline(
    entity_name: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    timeline = get_graph_store().get_entity_timeline(entity_name, limit=limit)
    return {
        "entity_name": entity_name,
        "timeline": timeline,
    }


@app.get("/temporal/query")
async def get_temporal_query(
    query: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    engine = _get_temporal_engine()
    return await asyncio.to_thread(engine.query_period, query, limit=limit)


@app.get("/temporal/briefing")
async def get_temporal_briefing(
    query: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    engine = _get_temporal_engine()
    payload = await asyncio.to_thread(engine.query_period, query, limit=limit)
    briefing = await synthesize_temporal_briefing(query, payload)
    return {
        **payload,
        "briefing": briefing,
    }


@app.get("/graph/evolution")
async def get_graph_evolution(
    topic: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    engine = _get_temporal_engine()
    return await asyncio.to_thread(engine.get_idea_evolution, topic, limit=limit)


@app.get("/graph/evolution/briefing")
async def get_graph_evolution_briefing(
    topic: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    engine = _get_temporal_engine()
    payload = await asyncio.to_thread(engine.get_idea_evolution, topic, limit=limit)
    briefing = await synthesize_evolution_briefing(topic, payload)
    return {
        **payload,
        "briefing": briefing,
    }


@app.get("/graph/communities")
async def get_graph_communities(
    limit: int = Query(default=12, ge=1, le=50),
    min_size: int = Query(default=3, ge=2, le=20),
    min_shared_mentions: int = Query(default=2, ge=1, le=10),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    communities = await asyncio.to_thread(
        get_graph_store().detect_communities,
        limit=limit,
        min_size=min_size,
        min_shared_mentions=min_shared_mentions,
    )
    return {
        "communities": communities,
        "count": len(communities),
    }


@app.get("/graph/community")
async def get_graph_community(
    community_id: str = Query(min_length=1, max_length=160),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    community = await asyncio.to_thread(get_graph_store().get_community, community_id)
    if community is None:
        raise HTTPException(status_code=404, detail="Community not found")
    return community


@app.get("/graph/community/briefing")
async def get_graph_community_briefing(
    community_id: str = Query(min_length=1, max_length=160),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    community = await asyncio.to_thread(get_graph_store().get_community, community_id)
    if community is None:
        raise HTTPException(status_code=404, detail="Community not found")
    sample_items = await asyncio.to_thread(
        _get_item_records,
        list(community.get("sample_episode_ids") or []),
    )
    briefing = await synthesize_community_briefing(
        {
            **community,
            "sample_items": sample_items,
        }
    )
    return {
        **community,
        "sample_items": sample_items,
        "briefing": briefing,
    }


@app.get("/folders/discover")
async def api_discover_folders(user: dict = Depends(require_access)) -> dict[str, Any]:
    return await asyncio.to_thread(get_cached_discovery)


@app.get("/folders/watched")
async def api_get_watched_folders(
    user: dict = Depends(require_access),
) -> list[dict[str, Any]]:
    conn = database.get_connection()
    try:
        return database.get_watched_folders(conn)
    finally:
        conn.close()


@app.post("/folders/estimate")
async def estimate_folder(
    data: FolderEstimateRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    raw_folders = data.folders
    if raw_folders:
        folder_inputs = [str(path).strip() for path in raw_folders if str(path).strip()]
        if not folder_inputs:
            raise HTTPException(status_code=400, detail="At least one folder path is required")

        estimates = await asyncio.gather(
            *(asyncio.to_thread(_estimate_folder_contents, raw_path) for raw_path in folder_inputs)
        )
        return _combine_estimate_results(list(estimates))

    raw_path = str(data.path or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Path is required")

    return await asyncio.to_thread(_estimate_folder_contents, raw_path)


@app.post("/folders/add")
async def api_add_folder(
    body: FolderRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    global _is_scanning, _scan_indexed, _scan_total, _scan_percent, _scan_start_count, _scan_phase_state

    raw_path = body.path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Path is required")

    path = Path(raw_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail="Path does not exist")

    access_probe = _probe_folder_access(path)
    if not access_probe["accessible"]:
        status_code = 409 if access_probe["permission_required"] else 400
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": (
                    "FOLDER_PERMISSION_REQUIRED"
                    if access_probe["permission_required"]
                    else "FOLDER_UNAVAILABLE"
                ),
                "message": access_probe["message"],
                "path": str(path),
            },
        )

    label = (body.label or path.name or str(path)).strip()

    conn = database.get_connection()
    try:
        database.add_watched_folder(conn, path, label)
    finally:
        conn.close()

    invalidate_discovery_cache()
    if _watcher is not None:
        _watcher.add_directory(path)

    if body.start_scan:
        _is_scanning = True
        _scan_indexed = 0
        _scan_total = 0
        _scan_percent = 0
        _scan_start_count = _get_item_count()
        _scan_phase_state = {
            "phase": "",
            "message": "",
            "found": 0,
            "checked": 0,
            "total": 0,
            "new_found": 0,
        }
        asyncio.create_task(_run_scan_for_paths([path]))

    return {"success": True, "path": str(path), "label": label}


@app.delete("/folders/remove")
async def api_remove_folder(
    body: FolderRequest,
    user: dict = Depends(require_access),
) -> dict[str, bool]:
    raw_path = body.path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Path is required")

    path = Path(raw_path).expanduser().resolve()

    conn = database.get_connection()
    try:
        row = conn.execute(
            """
            SELECT is_default
            FROM watched_folders
            WHERE path = ?
            """,
            (str(path),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        if int(row["is_default"] or 0):
            raise HTTPException(status_code=400, detail="Default folders cannot be removed")

        database.remove_watched_folder(conn, path)
    finally:
        conn.close()

    invalidate_discovery_cache()
    if _watcher is not None:
        _watcher.remove_directory(path)

    return {"success": True}


@app.post("/folders/open-picker")
async def api_open_folder_picker(
    user: dict = Depends(require_access),
) -> dict[str, str | None]:
    def _pick_folder() -> dict[str, str | None]:
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'POSIX path of (choose folder with prompt "Choose a folder for Recurdo to watch:")',
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception:
            return {"path": None}

        path = result.stdout.strip().rstrip("/")
        if path and Path(path).exists():
            return {"path": path, "label": Path(path).name}
        return {"path": None}

    return await asyncio.to_thread(_pick_folder)


@app.post("/search")
async def search_items(
    body: SearchRequest,
    explain: bool = False,
    user: dict = Depends(require_access),
) -> Any:
    _mark_query_activity()
    debug_enabled = bool(explain or body.explain)
    if debug_enabled:
        results, debug_payload = await search.search(
            body.query,
            max(body.limit * 3, body.limit),
            return_debug=True,
            case_id=body.case_id,
        )
    else:
        results = await search.search(
            body.query,
            max(body.limit * 3, body.limit),
            case_id=body.case_id,
        )
        debug_payload = None
    parsed = query_parser.parse_query(body.query)
    serialized = _serialize_search_results(
        results,
        parsed=parsed,
        limit=body.limit * 2,  # give reranker headroom before final slice
        explain=debug_enabled,
    )

    # Personalized reranking: boost items the user has opened for this query
    if RECURDO_RERANKER_ENABLED:
        try:
            user_id = str(user.get("id") or "local")
            conn = database.get_connection()
            candidate_ids = [str(r.get("id") or "") for r in serialized if r.get("id")]
            boost = personalize_module.build_boost_vector(
                conn, user_id, body.query, candidate_ids=candidate_ids
            )
            if boost:
                serialized = personalize_module.apply_boost(serialized, boost)
        except Exception:
            logger.debug("personalized reranker failed silently", exc_info=True)

    serialized = serialized[: body.limit]

    if not debug_enabled:
        return serialized

    return {
        "query": body.query,
        "debug": debug_payload or {},
        "results": serialized,
    }


@app.post("/search/briefing")
async def search_with_briefing(
    body: SearchRequest,
    explain: bool = False,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    _mark_query_activity()
    results = await search.search(
        body.query,
        max(body.limit * 3, body.limit),
        case_id=body.case_id,
    )
    parsed = query_parser.parse_query(body.query)
    response = _serialize_search_results(
        results,
        parsed=parsed,
        limit=body.limit,
        explain=bool(explain or body.explain),
    )
    briefing = await synthesize_search_briefing(body.query, response)
    return {
        "query": body.query,
        "briefing": briefing,
        "results": response,
    }


def _build_ask_source_pills(
    response: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Compact source payload for the /ask UI.

    The answer card only needs enough metadata to render a pill ([1] Title ·
    filename) and jump to the item on click. The full scoring breakdown that
    /search returns would just bloat the response and leak ranking internals
    into the chat surface.
    """
    pills: list[dict[str, Any]] = []
    for index, item in enumerate(response[:limit], start=1):
        snippet = item.get("summary") or item.get("preview_text") or ""
        if snippet and len(snippet) > 320:
            snippet = snippet[:317].rstrip() + "…"
        pills.append(
            {
                "index": index,
                "item_id": item.get("id"),
                "title": item.get("title") or item.get("filename") or item.get("id"),
                "filename": item.get("filename"),
                "source_type": item.get("source_type"),
                "modified_at": item.get("modified_at"),
                "snippet": snippet,
                "score": item.get("final_score"),
            }
        )
    return pills


def _log_ask_search_event(
    question: str,
    user_id: str,
    session_id: str | None,
) -> None:
    """Best-effort: log the /ask question as a 'search' interaction event.

    Mirrors the implicit-signal shape the batcher emits from the UI. Failures
    are swallowed — ask answers must not 500 because a side-channel insert
    hiccuped.
    """
    try:
        event = interactions_module.InteractionEventIn(
            event_type="search",
            query=question,
            session_id=session_id,
            client_timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"source": "ask"},
        )
        conn = database.get_connection()
        try:
            interactions_module.insert_interaction_events(
                conn, [event], user_id=user_id
            )
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover — best-effort log
        logger.debug("ask search-event mirror failed: %s", exc)


@app.post("/ask")
async def ask_question(
    body: AskRequest,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    """Conversational answer over the user's own memory.

    Flow:
      1. Run the hybrid retriever on the question (overfetching 3x).
      2. Serialize into the standard search payload so downstream consumers
         can cross-reference with /search results.
      3. Log an implicit 'search' event so the Phase 5 reranker learns which
         questions led to which clicks.
      4. Hand the top results to `synthesize_ask_answer` — which uses the
         LocalLLM 'quality' tier (Qwen3-4B / Gemini) and falls back to a
         deterministic top-3 list if the model stack is unavailable.
    """
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required")

    limit = max(1, min(int(body.limit or 10), 20))
    _mark_query_activity()

    results = await search.search(
        question,
        max(limit * 3, 30),
        case_id=body.case_id,
    )
    parsed = query_parser.parse_query(question)

    # For temporal queries ("yesterday", "today", "last week" etc.) blend in
    # date-sorted DB results so Gemini sees what was actually modified in that
    # window — not just semantically similar files from any time.
    # We fetch 3× more than needed and bucket by calendar date so one busy day
    # doesn't crowd out the rest of the week.
    if getattr(parsed, "time_window", None) is not None:
        try:
            tw = parsed.time_window
            conn_t = database.get_connection()
            since_ts = (
                tw.start_date.timestamp() if getattr(tw, "start_date", None)
                else (datetime.now(timezone.utc).timestamp() - 7 * 86400)
            )
            until_ts = (
                tw.end_date.timestamp() if getattr(tw, "end_date", None)
                else datetime.now(timezone.utc).timestamp()
            )
            # Fetch a wide pool, ordered by mtime desc
            fetch_limit = max(limit * 4, 40)
            temporal_where = """
                (file_mtime BETWEEN ? AND ? OR modified_at BETWEEN ? AND ?)
                AND is_excluded = 0
            """
            temporal_params: list[Any] = [since_ts, until_ts, since_ts, until_ts]
            if body.case_id:
                temporal_where += """
                    AND EXISTS (
                        SELECT 1
                        FROM case_evidence AS temporal_case_evidence
                        WHERE temporal_case_evidence.item_id = items.id
                          AND temporal_case_evidence.case_id = ?
                    )
                """
                temporal_params.append(body.case_id)
            temporal_params.append(fetch_limit)
            temporal_rows = conn_t.execute(
                f"""SELECT id, title, filename, file_type, source_type,
                          COALESCE(file_mtime, modified_at) AS ts,
                          preview_text, summary, original_path, extracted_text
                   FROM items
                   WHERE {temporal_where}
                   ORDER BY ts DESC
                   LIMIT ?""",
                temporal_params,
            ).fetchall()
            if temporal_rows:
                # Bucket by calendar date (UTC), take up to 3 per day so we get
                # coverage across the full window rather than just the busiest day.
                from collections import defaultdict
                buckets: dict = defaultdict(list)
                for row in temporal_rows:
                    ts_val = row[5]
                    day_key = (
                        datetime.fromtimestamp(ts_val, tz=timezone.utc).date().isoformat()
                        if ts_val else "unknown"
                    )
                    buckets[day_key].append(row)

                PER_DAY = 3
                sampled_rows = []
                for day in sorted(buckets.keys(), reverse=True):
                    sampled_rows.extend(buckets[day][:PER_DAY])

                temporal_hits = []
                for row in sampled_rows[:limit]:
                    ts_val = row[5]
                    mod_iso = (
                        datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
                        if ts_val else None
                    )
                    temporal_hits.append({
                        "id": row[0],
                        "title": row[1] or row[2] or "untitled",
                        "filename": row[2] or "",
                        "file_type": row[3] or "text",
                        "source_type": row[4] or "local",
                        "modified_at": mod_iso,
                        "preview_text": row[6] or row[7] or "",
                        "summary": row[7] or "",
                        "score": 1.0,
                        "original_path": row[8] or "",
                        "extracted_text": row[9] or "",
                    })

                seen_ids = {h["id"] for h in temporal_hits}
                deduped_semantic = [r for r in results if r.get("id") not in seen_ids]
                results = temporal_hits + deduped_semantic
        except Exception as _te:
            logger.debug("Temporal DB query failed: %s", _te)

    response = _serialize_search_results(
        results,
        parsed=parsed,
        limit=limit,
        explain=False,
    )

    user_id = str(user.get("id") or "local")
    asyncio.create_task(
        asyncio.to_thread(
            _log_ask_search_event, question, user_id, body.session_id
        )
    )

    email = str(user.get("email") or "")
    user_display_name = (
        str(user.get("user_metadata", {}).get("full_name") or "").strip()
        or str(user.get("name") or "").strip()
        or (email.split("@")[0] if email else None)
    )
    answer = await synthesize_ask_answer(
        question,
        response,
        allow_model=bool(body.allow_model),
        source_limit=min(limit, 10),
        conversation_history=[
            turn.model_dump()
            for turn in (body.conversation_history or [])
        ],
        user_display_name=user_display_name,
    )
    sources = _build_ask_source_pills(response, limit=limit)

    return {
        "question": question,
        "answer": {
            "text": answer.get("text", ""),
            "provider": answer.get("provider", "heuristic"),
            "confidence": answer.get("confidence", "low"),
        },
        "sources": sources,
        "result_count": len(response),
    }


@app.get("/items")
async def list_items(
    page: int = Query(default=1, ge=1, le=10000),
    limit: int = Query(default=20, ge=1, le=100),
    q: str = Query(default="", max_length=200),
    file_type: str = Query(default="", max_length=40, pattern=r"^[A-Za-z0-9_-]*$"),
    sort: Literal["recent", "name", "size"] = "recent",
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    _FILE_TYPE_MAP: dict[str, list[str]] = {
        "pdf": ["pdf"],
        "email": ["email"],
        "image": ["image", "jpg", "jpeg", "png", "gif", "webp"],
        "doc": ["doc", "docx", "txt", "md"],
        "spreadsheet": ["xlsx", "csv", "xls"],
        "presentation": ["pptx", "ppt"],
    }
    _SORT_MAP: dict[str, str] = {
        "recent": "file_mtime DESC",
        "name": "original_path ASC",
        "size": "file_size_bytes DESC",
    }

    where_clauses = ["is_excluded = 0"]
    params: list[Any] = []

    if q:
        where_clauses.append("original_path LIKE ?")
        params.append(f"%{q}%")

    if file_type and file_type in _FILE_TYPE_MAP:
        extensions = _FILE_TYPE_MAP[file_type]
        placeholders = ",".join("?" * len(extensions))
        where_clauses.append(f"file_type IN ({placeholders})")
        params.extend(extensions)

    order_by = _SORT_MAP.get(sort, "file_mtime DESC")
    where_sql = " AND ".join(where_clauses)

    offset = (page - 1) * limit
    conn = database.get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM items
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        if q or file_type:
            total = conn.execute(
                f"SELECT COUNT(*) FROM items WHERE {where_sql}",
                params,
            ).fetchone()[0]
        else:
            total = database.get_item_count(conn)
    finally:
        conn.close()

    return {
        "items": [dict(row) for row in rows if _item_exists_on_disk(dict(row))],
        "total": total,
        "page": page,
    }


@app.get("/activity/timeline")
async def get_activity_timeline(
    days: int = Query(default=30, ge=1, le=365),
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    cutoff = time.time() - days * 86400
    conn = database.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                id,
                original_path,
                file_type,
                file_mtime,
                preview_text,
                date(datetime(file_mtime, 'unixepoch')) AS day
            FROM items
            WHERE is_excluded = 0
              AND file_mtime >= ?
            ORDER BY file_mtime DESC
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    # Group by calendar date, keeping top 5 items per day
    from collections import defaultdict

    day_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        r = dict(row)
        day = r.pop("day", None)
        if day is None:
            continue
        if len(day_map[day]) < 5:
            day_map[day].append({
                "id": r["id"],
                "file_path": r["original_path"],
                "file_type": r["file_type"],
                "file_mtime": r["file_mtime"],
                "preview_text": r["preview_text"],
            })

    timeline = [
        {"date": day, "count": len(items), "items": items}
        for day, items in sorted(day_map.items(), reverse=True)
    ]

    return {
        "days": timeline,
        "total_days": len(timeline),
        "period_days": days,
    }


@app.delete("/items/all")
async def clear_all_items(user: dict = Depends(require_access)) -> dict[str, str]:
    del user

    if _is_scanning:
        raise HTTPException(
            status_code=409,
            detail="Cannot clear indexed data while a scan is running",
        )

    try:
        await asyncio.to_thread(_clear_library_state)
    except sqlite3.DatabaseError:
        logger.warning("Library reset hit a malformed database; recreating storage", exc_info=True)
        await asyncio.to_thread(_clear_library_state)
    except Exception as exc:
        logger.exception("Failed to reset local library")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "RESET_FAILED",
                "message": f"Unable to reset local library: {exc}",
            },
        ) from exc

    return {"status": "cleared"}


@app.get("/items/{item_id}")
async def get_item(
    item_id: str,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    item = _get_item_record(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    return item


@app.post("/access/{item_id}/{event_type}")
async def log_item_access(
    item_id: str,
    event_type: str,
    body: CognitiveAccessRequest | None = None,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    item = _get_item_record(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    normalized_event = str(event_type or "").strip().lower()
    normalized_event = {
        "opened": "open",
        "previewed": "preview",
        "copied": "copy",
    }.get(normalized_event, normalized_event)
    if not normalized_event:
        raise HTTPException(status_code=400, detail="Event type is required")

    payload = body or CognitiveAccessRequest()
    metadata = {
        **(payload.metadata or {}),
        "source": "api",
    }

    asyncio.create_task(
        asyncio.to_thread(
            _record_item_event_best_effort,
            item_id,
            normalized_event,
            duration_seconds=payload.duration_seconds,
        )
    )
    asyncio.create_task(
        asyncio.to_thread(
            _record_cognitive_event_best_effort,
            item_id,
            normalized_event,
            event_weight=float(payload.event_weight),
            metadata=metadata,
        )
    )

    # Dual-write into interaction_events so the Phase 3 Now feed and Phase 5
    # reranker see item-level opens/copies even when the client hasn't been
    # upgraded to the new /events/batch endpoint yet. Mapped to the
    # interaction taxonomy: 'preview' becomes 'dwell' so downstream consumers
    # do not need to know the legacy event name.
    interaction_event_type = {
        "open": "open",
        "preview": "dwell",
        "copy": "copy",
    }.get(normalized_event)
    if interaction_event_type is not None:
        dwell_ms: int | None = None
        if interaction_event_type == "dwell" and payload.duration_seconds:
            dwell_ms = int(float(payload.duration_seconds) * 1000)
        asyncio.create_task(
            asyncio.to_thread(
                _mirror_item_access_to_interactions,
                item_id,
                interaction_event_type,
                dwell_ms,
                dict(metadata),
            )
        )

    return {
        "status": "logged",
        "item_id": item_id,
        "event_type": normalized_event,
    }


def _mirror_item_access_to_interactions(
    item_id: str,
    event_type: str,
    dwell_ms: int | None,
    metadata: dict[str, Any],
) -> None:
    """Best-effort mirror of an /access event into interaction_events.

    Runs off the event loop via asyncio.to_thread. Failures are logged but
    never raised — the legacy /access write is authoritative for the
    cognitive/activation system; this is a secondary signal for ranking.
    """
    try:
        event = interactions_module.InteractionEventIn(
            event_type=event_type,
            item_id=item_id,
            dwell_ms=dwell_ms,
            metadata={**metadata, "source": "access_mirror"},
            client_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        conn = database.get_connection()
        try:
            interactions_module.insert_interaction_events(conn, [event])
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover — best-effort mirror
        logger.debug("interaction_events mirror failed: %s", exc)


# Batched interaction event ingestion. Client batches every ~5s or on visibility
# change and POSTs here. Rejection of a single bad event does not fail the
# batch; the response's `rejected` + `reasons` tells the client what to drop.
@app.post("/events/batch")
async def record_interaction_events_batch(
    payload: interactions_module.InteractionBatchIn,
    user: dict = Depends(require_access),
) -> interactions_module.InteractionBatchOut:
    user_id = str(user.get("id") or "local")
    default_session_id = payload.session_id

    def _do_insert() -> tuple[int, list[str]]:
        conn = database.get_connection()
        try:
            return interactions_module.insert_interaction_events(
                conn,
                payload.events,
                user_id=user_id,
                default_session_id=default_session_id,
            )
        finally:
            conn.close()

    accepted, reasons = await asyncio.to_thread(_do_insert)
    rejected = len(payload.events) - accepted
    return interactions_module.InteractionBatchOut(
        accepted=accepted,
        rejected=rejected,
        reasons=reasons,
    )


# ── Predictive "Now" feed ────────────────────────────────────────────────────
# TTL cache for the Now feed. The feed's inputs (interaction_events,
# items.modified_at) change slowly relative to how often the side panel polls,
# so a small per-user cache cuts SQLite read pressure by ~20x without making
# the feed feel stale. Cache is keyed on user_id so distinct users (a future
# shared-mode Recurdo instance) don't leak each other's history.
_NOW_FEED_CACHE_TTL_SECONDS = 20.0
_now_feed_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_now_feed_lock = asyncio.Lock()


def _compute_now_feed_sync(user_id: str, limit: int) -> list[dict[str, Any]]:
    """Blocking scoring pass — runs under asyncio.to_thread so FAISS/SQLite
    reads don't stall the event loop."""
    conn = database.get_connection()
    try:
        return now_feed_module.build_now_feed(
            conn,
            user_id=user_id,
            limit=limit,
        )
    finally:
        conn.close()


@app.get("/now")
async def get_now_feed(
    limit: int = Query(default=10, ge=1, le=30),
    refresh: bool = False,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    """Proactive feed: what the user probably wants, without a query.

    Response shape:
      {
        "items": [...],        # scored + diversified candidates
        "count": N,
        "generated_at": ISO,
        "cached": bool,
      }

    The feed is cached for ~20s per user. Pass `refresh=true` to bypass the
    cache — used by the UI after an explicit dismiss so the dismissed card
    doesn't immediately re-appear from cache.
    """
    user_id = str(user.get("id") or "local")
    clamped_limit = limit

    now_ts = time.time()
    if not refresh:
        cached = _now_feed_cache.get(user_id)
        if cached and (now_ts - cached[0]) < _NOW_FEED_CACHE_TTL_SECONDS:
            return {
                "items": cached[1][:clamped_limit],
                "count": min(len(cached[1]), clamped_limit),
                "generated_at": datetime.fromtimestamp(cached[0], tz=timezone.utc).isoformat(),
                "cached": True,
            }

    async with _now_feed_lock:
        # Double-check after acquiring the lock: another caller may have
        # refreshed the cache while we were queued.
        if not refresh:
            cached = _now_feed_cache.get(user_id)
            if cached and (time.time() - cached[0]) < _NOW_FEED_CACHE_TTL_SECONDS:
                return {
                    "items": cached[1][:clamped_limit],
                    "count": min(len(cached[1]), clamped_limit),
                    "generated_at": datetime.fromtimestamp(cached[0], tz=timezone.utc).isoformat(),
                    "cached": True,
                }

        items = await asyncio.to_thread(
            _compute_now_feed_sync, user_id, max(clamped_limit, 15),
        )
        captured_at = time.time()
        _now_feed_cache[user_id] = (captured_at, items)

    return {
        "items": items[:clamped_limit],
        "count": min(len(items), clamped_limit),
        "generated_at": datetime.fromtimestamp(captured_at, tz=timezone.utc).isoformat(),
        "cached": False,
    }


@app.get("/item/{item_id}/activation")
async def get_item_activation(
    item_id: str,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    item = _get_item_record(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    store = cognitive_store.get_store()
    access_history = store.get_activation_events(item_id)
    weighted_history = store.get_activation_history(item_id)
    base_level = compute_base_level_activation(weighted_history)
    normalized = normalize_activation(base_level)
    probability = retrieval_probability(base_level) if math.isfinite(base_level) else 0.0

    return {
        "item_id": item_id,
        "title": item.get("title") or item.get("filename") or item_id,
        "base_level_activation": round(base_level, 4) if math.isfinite(base_level) else None,
        "normalized_activation": round(normalized, 4),
        "retrieval_probability": round(probability, 4),
        "event_count": len(access_history),
        "last_event_at": access_history[0]["occurred_at"] if access_history else None,
        "access_history": access_history,
    }


@app.get("/item/{item_id}/frame")
async def get_item_frame(
    item_id: str,
    user: dict = Depends(require_access),
) -> dict[str, Any]:
    del user
    item = _get_item_record(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    frame = cognitive_store.get_store().get_frame(item_id)
    return {
        "item_id": item_id,
        "title": item.get("title") or item.get("filename") or item_id,
        "frame_type": frame.get("frame_type") if frame else None,
        "slots": frame.get("slots") if frame else {},
        "confidence": round(float(frame.get("confidence") or 0.0), 3) if frame else 0.0,
        "updated_at": frame.get("updated_at") if frame else None,
    }


@app.post("/items/{item_id}/open")
async def log_item_open(
    item_id: str,
    user: dict = Depends(require_access),
) -> dict[str, str]:
    item = _get_item_record(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    original_target = str(item.get("original_path") or "").strip()
    if str(item.get("source_type") or "").strip().lower() in {"gmail", "gmail_attachment"}:
        if not _is_url_target(original_target):
            raise HTTPException(status_code=404, detail="Gmail message URL is unavailable")
        asyncio.create_task(asyncio.to_thread(_open_url_target, original_target))
    else:
        original_path = Path(original_target)
        if not original_path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")
        asyncio.create_task(asyncio.to_thread(_open_local_file, original_path))
    asyncio.create_task(asyncio.to_thread(_log_item_open_best_effort, item_id))

    return {"status": "opened"}


@app.post("/items/{item_id}/log-open")
async def log_item_open_only(
    item_id: str,
    user: dict = Depends(require_access),
) -> dict[str, str]:
    asyncio.create_task(asyncio.to_thread(_log_item_open_best_effort, item_id))
    return {"status": "logged"}


@app.post("/items/{item_id}/reveal")
async def reveal_item(
    item_id: str,
    user: dict = Depends(require_access),
) -> dict[str, str]:
    conn = database.get_connection()
    try:
        item = database.get_item_by_id(conn, item_id)
    finally:
        conn.close()

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    original_target = str(item.get("original_path") or "").strip()
    if str(item.get("source_type") or "").strip().lower() in {"gmail", "gmail_attachment"}:
        if not _is_url_target(original_target):
            raise HTTPException(status_code=404, detail="Gmail message URL is unavailable")
        asyncio.create_task(asyncio.to_thread(_open_url_target, original_target))
    else:
        original_path = Path(original_target)
        if not original_path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")
        asyncio.create_task(asyncio.to_thread(_reveal_local_file, original_path))

    return {"status": "revealed"}


@app.delete("/items/{item_id}")
async def exclude_item(
    item_id: str,
    user: dict = Depends(require_access),
) -> dict[str, str]:
    conn = database.get_connection()
    try:
        database.update_item(conn, item_id, {"is_excluded": 1})
        conn.commit()
    finally:
        conn.close()

    return {"status": "excluded"}


@app.post("/admin/config")
async def admin_config(body: AdminConfigRequest, request: Request) -> dict[str, Any]:
    """Creator-only config endpoint. Disabled unless explicitly enabled."""
    enabled = os.getenv("RECURDO_ENABLE_ADMIN_CONFIG", "").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        raise HTTPException(status_code=404, detail="Not found")

    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="forbidden")

    expected_token = os.getenv("RECURDO_ADMIN_CONFIG_TOKEN", "").strip()
    if expected_token:
        provided = request.headers.get("X-Recurdo-Admin-Token", "").strip()
        if not (provided and secrets.compare_digest(provided, expected_token)):
            raise HTTPException(status_code=403, detail="forbidden")

    def clean_env_value(raw_value: str, field_name: str) -> str:
        value = raw_value.strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"{field_name} cannot be empty")
        if any(char in value for char in ("\n", "\r", "\0")):
            raise HTTPException(status_code=400, detail=f"{field_name} contains invalid characters")
        return value

    allowed_models = {
        "gemini-3.8-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash-8b",
    }

    env_path = config.USER_BACKEND_ENV_PATH
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text() if env_path.exists() else ""
    lines = existing.splitlines()

    updated: dict[str, str] = {}
    if body.gemini_key:
        key = clean_env_value(body.gemini_key, "gemini_key")
        updated["GEMINI_API_KEY"] = key
        config.GEMINI_API_KEY = key
        os.environ["GEMINI_API_KEY"] = key
    if body.model:
        model_name = clean_env_value(body.model, "model")
        if model_name not in allowed_models:
            raise HTTPException(status_code=400, detail="Unsupported Gemini model")
        updated["ENRICHMENT_MODEL"] = model_name
        config.ENRICHMENT_MODEL = model_name
        os.environ["ENRICHMENT_MODEL"] = model_name

    # Rewrite env file
    if updated:
        new_lines = []
        written_keys: set[str] = set()
        for line in lines:
            key_part = line.split("=", 1)[0]
            if key_part in updated:
                new_lines.append(f"{key_part}={updated[key_part]}")
                written_keys.add(key_part)
            else:
                new_lines.append(line)
        for k, v in updated.items():
            if k not in written_keys:
                new_lines.append(f"{k}={v}")
        env_path.write_text("\n".join(new_lines) + "\n")

        # Reset Gemini model cache
        try:
            from llm import local_llm as _local_llm_module
            _local_llm_module._gemini_configured = False
            _local_llm_module._gemini_models.clear()
        except Exception:
            pass

    return {
        "status": "ok",
        "applied": list(updated.keys()),
        "model": config.ENRICHMENT_MODEL,
        "gemini_configured": bool(config.GEMINI_API_KEY and config.GEMINI_API_KEY.strip()),
    }


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    await websocket.accept()
    _websocket_clients.add(websocket)
    if _startup_health_state is not None:
        try:
            await websocket.send_json(_startup_health_state)
        except Exception:
            _websocket_clients.discard(websocket)
            with suppress(Exception):
                await websocket.close()
            return
    if _cognitive_bootstrap_state.get("state") != "idle":
        try:
            await websocket.send_json(
                {
                    "type": "cognitive_bootstrap",
                    **dict(_cognitive_bootstrap_state),
                }
            )
        except Exception:
            _websocket_clients.discard(websocket)
            with suppress(Exception):
                await websocket.close()
            return
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _websocket_clients.discard(websocket)
    finally:
        _websocket_clients.discard(websocket)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.PORT, reload=False)


if __name__ == "__main__":
    main()
