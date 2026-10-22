from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer

try:
    from .. import config
except ImportError:
    import config


DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "me",
    "more",
    "most",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "same",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
}

# Nomic-embed-text-v1.5 on Apple Silicon (M-series) saturates at batch=64 on
# both CPU and MPS without quality loss; the prior 32 was the bottleneck for
# initial library scans. Env-overridable for older/Intel hardware where higher
# batches can increase tail latency — set RECURDO_EMBED_BATCH_SIZE=32 to revert.
def _resolve_embed_batch_size() -> int:
    raw = os.getenv("RECURDO_EMBED_BATCH_SIZE")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 64


EMBED_BATCH_SIZE = _resolve_embed_batch_size()
CHUNK_EMBED_BATCH_SIZE = EMBED_BATCH_SIZE
FULL_CHUNK_EMBED_THRESHOLD = 1024
MAX_CHUNKS_PER_DOC = 128
SUMMARY_EMBED_MAX_CHARS = 2000
SUMMARY_EMBED_WEIGHT = 0.9
DEFAULT_INDEX_MODEL_POOL_SIZE = 2
DEFAULT_QUERY_MODEL_POOL_SIZE = 1
EMBED_MODEL_INSTANCE_SIZE_GB = 0.55
EMBED_MODEL_HEADROOM_GB = 0.25
SHARE_MODEL_POOL = os.getenv("RECURDO_SHARE_EMBEDDING_MODEL", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

logger = logging.getLogger(__name__)

_text_index: "TextFaissIndex | None" = None
_faiss_module: Any | None = None
_model_pool_warmup_task: asyncio.Task[None] | None = None
BUNDLED_MODEL_SUBDIR = Path("models") / "nomic-embed-text-v1.5"
BUNDLED_MODEL_REQUIRED_FILES = (
    "config.json",
    "modules.json",
    "model.safetensors",
    "configuration_hf_nomic_bert.py",
    "modeling_hf_nomic_bert.py",
)


def _faiss() -> Any:
    global _faiss_module

    if _faiss_module is None:
        import faiss

        _faiss_module = faiss

    return _faiss_module


def _is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))


def _bundled_model_candidates() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    raw_meipass = getattr(sys, "_MEIPASS", None)
    if raw_meipass:
        for candidate in (
            Path(raw_meipass) / BUNDLED_MODEL_SUBDIR,
            Path(raw_meipass) / "_internal" / BUNDLED_MODEL_SUBDIR,
        ):
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (
            exe_dir / BUNDLED_MODEL_SUBDIR,
            exe_dir / "_internal" / BUNDLED_MODEL_SUBDIR,
        ):
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

    return candidates


def resolve_embedding_model_target(model_name: str | None = None) -> str:
    configured_name = model_name or config.EMBEDDING_MODEL

    if not _is_frozen_runtime():
        return configured_name

    for candidate in _bundled_model_candidates():
        missing = [
            filename
            for filename in BUNDLED_MODEL_REQUIRED_FILES
            if not (candidate / filename).exists()
        ]
        if not missing:
            return str(candidate)

    searched = ", ".join(str(path) for path in _bundled_model_candidates()) or "none"
    raise FileNotFoundError(
        "Bundled embedding model is incomplete for packaged runtime. "
        f"Searched: {searched}. "
        "Run scripts/pre_bundle_model.py before building the packaged app."
    )


class EmbedModelPool:
    def __init__(
        self,
        model_name: str,
        *,
        pool_size: int = DEFAULT_INDEX_MODEL_POOL_SIZE,
        max_seq_length: int = 512,
        label: str = "embed",
    ) -> None:
        self.model_name = model_name
        self.label = label
        self.requested_pool_size = max(1, int(pool_size))
        self.pool_size = max(1, int(pool_size))
        self.max_seq_length = max_seq_length
        self._pool: queue.Queue[SentenceTransformer] | None = None
        self._initialized = False
        self._init_lock = threading.Lock()

    @property
    def initialized(self) -> bool:
        return self._initialized

    @staticmethod
    def _best_device() -> str:
        forced = os.getenv("RECURDO_EMBEDDINGS_DEVICE", "").strip().lower()
        if forced in {"cpu", "mps"}:
            return forced

        # Packaged builds should default to CPU for stability. Some macOS
        # systems intermittently fail to reach MTLCompilerService under
        # hardened runtime, which can crash MPS kernels during indexing.
        if _is_frozen_runtime():
            return "cpu"

        try:
            import torch
            if torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def _load_one_model(self) -> SentenceTransformer:
        model_target = resolve_embedding_model_target(self.model_name)
        packaged_runtime = _is_frozen_runtime()
        device = self._best_device()
        try:
            model = SentenceTransformer(
                model_target,
                trust_remote_code=True,
                device=device,
                local_files_only=packaged_runtime,
            )
        except Exception as exc:
            if packaged_runtime:
                raise RuntimeError(
                    f"Failed to load bundled embedding model from {model_target}"
                ) from exc
            print("Downloading Nomic Embed model (274MB) — one time only...")
            model = SentenceTransformer(
                self.model_name,
                trust_remote_code=True,
                device=device,
            )
        model.max_seq_length = self.max_seq_length
        return model

    def _memory_snapshot_gb(self) -> tuple[float | None, float | None]:
        try:
            import psutil  # type: ignore

            virtual_memory = psutil.virtual_memory()
            return (
                float(virtual_memory.total) / 1024**3,
                float(virtual_memory.available) / 1024**3,
            )
        except Exception:
            pass

        if sys.platform == "darwin":
            try:
                total_bytes = int(
                    subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()
                )
                vm_stat = subprocess.check_output(["vm_stat"]).decode()
                pages: dict[str, int] = {"free": 0, "inactive": 0, "speculative": 0}
                for line in vm_stat.splitlines():
                    if "Pages free" in line:
                        pages["free"] = int(line.split(":")[1].strip().rstrip("."))
                    elif "Pages inactive" in line:
                        pages["inactive"] = int(
                            line.split(":")[1].strip().rstrip(".")
                        )
                    elif "Pages speculative" in line:
                        pages["speculative"] = int(
                            line.split(":")[1].strip().rstrip(".")
                        )
                available_bytes = (
                    pages["free"] + pages["inactive"] + pages["speculative"]
                ) * 4096
                return (
                    float(total_bytes) / 1024**3,
                    float(available_bytes) / 1024**3,
                )
            except Exception:
                return None, None

        return None, None

    def _choose_pool_size(self) -> tuple[int, float | None, float | None]:
        total_gb, available_gb = self._memory_snapshot_gb()
        actual = self.requested_pool_size

        if total_gb is not None:
            if total_gb < 12:
                actual = min(actual, 1)
            elif total_gb < 24:
                actual = min(actual, 2)
            elif total_gb < 32:
                actual = min(actual, 3)

        if available_gb is not None:
            while (
                actual > 1
                and available_gb
                < (actual * EMBED_MODEL_INSTANCE_SIZE_GB) + EMBED_MODEL_HEADROOM_GB
            ):
                actual -= 1

        return max(1, actual), total_gb, available_gb

    def ensure_initialized_sync(self) -> None:
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            actual_size, total_gb, available_gb = self._choose_pool_size()
            if actual_size < self.requested_pool_size:
                total_label = (
                    f"{total_gb:.1f}GB" if total_gb is not None else "unknown"
                )
                available_label = (
                    f"{available_gb:.1f}GB" if available_gb is not None else "unknown"
                )
                print(
                    f"[EMBED_POOL] Reducing pool size from {self.requested_pool_size} "
                    f"to {actual_size} (total RAM: {total_label}, available RAM: {available_label})"
                )

            self.pool_size = actual_size
            self._pool = queue.Queue(maxsize=self.pool_size)
            print(
                f"[EMBED_POOL:{self.label}] Loading {self.pool_size} model instances..."
            )

            for index in range(self.pool_size):
                t0 = time.perf_counter()
                model = self._load_one_model()
                self._pool.put(model)
                elapsed = time.perf_counter() - t0
                print(
                    f"[EMBED_POOL:{self.label}] Model {index + 1}/{self.pool_size} ready "
                    f"({elapsed:.1f}s)"
                )

            self._initialized = True
            print(
                f"[EMBED_POOL:{self.label}] Pool ready — {self.pool_size} instances available"
            )

    async def initialize(self) -> None:
        await asyncio.to_thread(self.ensure_initialized_sync)

    @contextmanager
    def acquire_sync(self):
        self.ensure_initialized_sync()
        assert self._pool is not None
        model = self._pool.get()
        try:
            yield model
        finally:
            self._pool.put(model)

    @asynccontextmanager
    async def acquire(self):
        await self.initialize()
        assert self._pool is not None
        model = await asyncio.to_thread(self._pool.get)
        try:
            yield model
        finally:
            self._pool.put(model)

_index_model_pool = EmbedModelPool(
    config.EMBEDDING_MODEL,
    pool_size=1 if SHARE_MODEL_POOL else DEFAULT_INDEX_MODEL_POOL_SIZE,
    max_seq_length=512,
    label="index",
)
_query_model_pool = (
    _index_model_pool
    if SHARE_MODEL_POOL
    else EmbedModelPool(
        config.EMBEDDING_MODEL,
        pool_size=DEFAULT_QUERY_MODEL_POOL_SIZE,
        max_seq_length=512,
        label="query",
    )
)


async def initialize_model_pool() -> None:
    await _index_model_pool.initialize()
    await _query_model_pool.initialize()


def ensure_model_pool_warmup_started() -> asyncio.Task[None] | None:
    global _model_pool_warmup_task

    if _index_model_pool.initialized and _query_model_pool.initialized:
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    if _model_pool_warmup_task is not None and not _model_pool_warmup_task.done():
        return _model_pool_warmup_task

    async def _warmup() -> None:
        try:
            await initialize_model_pool()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background model pool warmup failed")

    task = loop.create_task(_warmup())

    def _clear(done_task: asyncio.Task[None]) -> None:
        global _model_pool_warmup_task
        if _model_pool_warmup_task is done_task:
            _model_pool_warmup_task = None

    task.add_done_callback(_clear)
    _model_pool_warmup_task = task
    return task


def get_model_pool_status() -> dict[str, Any]:
    resolved_target: str | None = None
    try:
        resolved_target = resolve_embedding_model_target(config.EMBEDDING_MODEL)
    except FileNotFoundError:
        resolved_target = None

    return {
        "initialized": _index_model_pool.initialized and _query_model_pool.initialized,
        "pool_size": _index_model_pool.pool_size,
        "requested_pool_size": _index_model_pool.requested_pool_size,
        "index_pool_size": _index_model_pool.pool_size,
        "index_pool_requested_size": _index_model_pool.requested_pool_size,
        "query_pool_initialized": _query_model_pool.initialized,
        "query_pool_size": _query_model_pool.pool_size,
        "query_pool_requested_size": _query_model_pool.requested_pool_size,
        "packaged_runtime": _is_frozen_runtime(),
        "resolved_model_target": resolved_target,
    }


class TextFaissIndex:
    def __init__(self, index_path: Path, dim: int = 768) -> None:
        self.index_path = Path(index_path)
        self.dim = dim
        self.index: Any | None = None
        self._count = 0
        self._lock = threading.RLock()

    def load(self) -> None:
        with self._lock:
            faiss = _faiss()
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            if self.index_path.exists():
                self.index = faiss.read_index(str(self.index_path))
            else:
                self.index = faiss.IndexFlatIP(self.dim)
            self._count = int(self.index.ntotal)

    def add(self, vectors: np.ndarray) -> list[int]:
        with self._lock:
            faiss = _faiss()
            if self.index is None:
                self.load()

            prepared = np.asarray(vectors, dtype=np.float32)
            if prepared.ndim == 1:
                prepared = prepared.reshape(1, -1)
            if prepared.shape[1] != self.dim:
                raise ValueError(
                    f"Expected vectors with dimension {self.dim}, got {prepared.shape[1]}"
                )

            faiss.normalize_L2(prepared)
            start_id = self._count
            self.index.add(prepared)
            self._count += len(prepared)

            if self._count % 50 == 0:
                self.save()

            return list(range(start_id, self._count))

    def search(
        self, query_vector: np.ndarray, k: int = 40
    ) -> tuple[list[int], list[float]]:
        with self._lock:
            faiss = _faiss()
            if self.index is None:
                self.load()
            if self.total == 0:
                return [], []

            prepared = np.asarray(query_vector, dtype=np.float32).reshape(1, -1)
            if prepared.shape[1] != self.dim:
                raise ValueError(
                    f"Expected query vector with dimension {self.dim}, got {prepared.shape[1]}"
                )

            faiss.normalize_L2(prepared)
            scores, ids = self.index.search(prepared, k)
            valid = [
                (int(index_id), float(score))
                for index_id, score in zip(ids[0], scores[0])
                if index_id != -1
            ]
            return [value[0] for value in valid], [value[1] for value in valid]

    def save(self) -> None:
        with self._lock:
            faiss = _faiss()
            if self.index is None:
                self.load()
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: stage to a sibling tmp file, then rename into place.
            # Prevents a corrupt FAISS file if the process dies mid-write.
            tmp_path = self.index_path.with_name(
                self.index_path.name + ".tmp"
            )
            try:
                faiss.write_index(self.index, str(tmp_path))
                os.replace(tmp_path, self.index_path)
            except Exception:
                # Best-effort cleanup of a partial tmp file so we do not
                # leave stray .tmp artifacts on disk.
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                raise

    def reset(self) -> None:
        with self._lock:
            faiss = _faiss()
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self.index = faiss.IndexFlatIP(self.dim)
            self._count = 0
            faiss.write_index(self.index, str(self.index_path))

    @property
    def total(self) -> int:
        return self._count


def get_text_index() -> TextFaissIndex:
    global _text_index

    if _text_index is None:
        _text_index = TextFaissIndex(
            config.TEXT_FAISS_PATH,
            dim=config.TEXT_EMBEDDING_DIM,
        )
        _text_index.load()

    return _text_index


def save_text_index_if_loaded() -> None:
    if _text_index is not None:
        _text_index.save()


def _normalize_rows(vectors: np.ndarray) -> np.ndarray:
    prepared = np.asarray(vectors, dtype=np.float32)
    if prepared.ndim == 1:
        prepared = prepared.reshape(1, -1)

    norms = np.linalg.norm(prepared, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (prepared / norms).astype(np.float32, copy=False)


def _document_input(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith(DOCUMENT_PREFIX) or stripped.startswith(QUERY_PREFIX):
        return stripped
    return f"{DOCUMENT_PREFIX}{stripped}"


def _query_input(query: str) -> str:
    stripped = (query or "").strip()
    if stripped.startswith(DOCUMENT_PREFIX) or stripped.startswith(QUERY_PREFIX):
        return stripped
    return f"{QUERY_PREFIX}{stripped}"


def _extract_keywords(text: str, limit: int = 20) -> tuple[dict[str, float], list[str]]:
    tokens = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{1,}\b", text.lower())
    counts = Counter(
        token
        for token in tokens
        if token not in STOPWORDS and not token.isdigit() and len(token) > 2
    )
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    keyword_weights = {token: float(count) for token, count in ranked}
    top_keywords = [token for token, _ in ranked]
    return keyword_weights, top_keywords


def chunk_text(text: str, chunk_size: int = 4000, overlap: int = 400) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0:
        raise ValueError("overlap cannot be negative")
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    chunks: list[str] = []
    start = 0
    step = chunk_size - overlap

    while start < len(stripped):
        end = start + chunk_size
        chunk = stripped[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start += step

    return chunks


def _limit_document_chunks(
    chunks: list[str],
    *,
    source_label: str = "",
) -> tuple[list[str], int]:
    original_count = len(chunks)
    if original_count <= MAX_CHUNKS_PER_DOC:
        return chunks, original_count

    label = source_label or "document"
    print(
        f"[EMBED] {label}: {original_count} chunks -> "
        f"truncated to {MAX_CHUNKS_PER_DOC}"
    )
    return chunks[:MAX_CHUNKS_PER_DOC], original_count


def _document_summary_text(
    summary_text: str | None,
    normalized_text: str,
    chunk_source_text: str,
) -> str:
    preferred = str(summary_text or "").strip()
    if preferred:
        return preferred[:SUMMARY_EMBED_MAX_CHARS]

    fallback = chunk_source_text or normalized_text
    return str(fallback or "").strip()[:SUMMARY_EMBED_MAX_CHARS]


def embed_text(text: str) -> np.ndarray:
    vectors = embed_batch([text])
    return vectors[0]


def _encode_with_audit(
    model: SentenceTransformer,
    inputs: str | list[str],
    *,
    batch_size: int | None = None,
    source_label: str = "",
) -> np.ndarray:
    input_count = 1 if isinstance(inputs, str) else len(inputs)
    requested_batch_size = batch_size if batch_size is not None else "default"
    thread_name = threading.current_thread().name
    source_suffix = f" source={source_label}" if source_label else ""

    print(
        f"[EMBED] thread={thread_name} "
        f"n={input_count} requested_batch_size={requested_batch_size}{source_suffix} start"
    )
    t0 = time.perf_counter()

    encode_kwargs: dict[str, Any] = {
        "convert_to_numpy": True,
        "normalize_embeddings": True,
        "show_progress_bar": False,
    }
    if batch_size is not None:
        encode_kwargs["batch_size"] = batch_size

    vectors = model.encode(inputs, **encode_kwargs)
    elapsed = time.perf_counter() - t0
    per_item = elapsed / input_count if input_count else 0.0

    print(
        f"[EMBED] thread={thread_name} "
        f"n={input_count} requested_batch_size={requested_batch_size} "
        f"{source_suffix} done elapsed={elapsed:.3f}s"
    )
    print(
        f"[EMBED_BATCH] n={input_count}  "
        f"requested_batch_size={requested_batch_size}  "
        f"{f'source={source_label}  ' if source_label else ''}"
        f"elapsed={elapsed:.3f}s  "
        f"per_item={per_item:.3f}s"
    )
    return vectors


def _embed_texts_sync(texts: list[str], *, source_label: str = "") -> np.ndarray:
    if not texts:
        return np.empty((0, config.TEXT_EMBEDDING_DIM), dtype=np.float32)

    prefixed = [_document_input(text) for text in texts]
    total_batches = max(1, (len(prefixed) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE)
    all_vectors: list[np.ndarray] = []

    with _index_model_pool.acquire_sync() as model:
        for batch_index, start in enumerate(
            range(0, len(prefixed), EMBED_BATCH_SIZE), start=1
        ):
            batch = prefixed[start : start + EMBED_BATCH_SIZE]
            batch_label = (
                f"{source_label} batch {batch_index}/{total_batches}"
                if source_label and total_batches > 1
                else source_label
            )
            vectors = _encode_with_audit(
                model,
                batch,
                batch_size=EMBED_BATCH_SIZE,
                source_label=batch_label,
            )
            all_vectors.append(np.asarray(vectors, dtype=np.float32))

    return _normalize_rows(np.vstack(all_vectors))


def embed_batch(texts: list[str]) -> np.ndarray:
    return _embed_texts_sync(texts)


async def embed_texts_async(
    texts: list[str],
    source_label: str = "",
) -> np.ndarray:
    if not texts:
        return np.empty((0, config.TEXT_EMBEDDING_DIM), dtype=np.float32)

    loop = asyncio.get_running_loop()
    prefixed = [_document_input(text) for text in texts]
    total_batches = max(1, (len(prefixed) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE)
    all_vectors: list[np.ndarray] = []

    async with _index_model_pool.acquire() as model:
        for batch_index, start in enumerate(
            range(0, len(prefixed), EMBED_BATCH_SIZE), start=1
        ):
            batch = prefixed[start : start + EMBED_BATCH_SIZE]
            batch_label = (
                f"{source_label} batch {batch_index}/{total_batches}"
                if source_label and total_batches > 1
                else source_label
            )
            batch_vectors = await loop.run_in_executor(
                None,
                partial(
                    _encode_with_audit,
                    model,
                    batch,
                    batch_size=EMBED_BATCH_SIZE,
                    source_label=batch_label,
                ),
            )
            all_vectors.append(np.asarray(batch_vectors, dtype=np.float32))
            await asyncio.sleep(0)

    return _normalize_rows(np.vstack(all_vectors))


def _store_faiss_embeddings(
    conn: sqlite3.Connection,
    item_id: str,
    vectors: np.ndarray,
    *,
    embedding_type: str,
    created_at: str,
) -> list[int]:
    prepared = np.asarray(vectors, dtype=np.float32)
    if prepared.size == 0:
        return []

    if prepared.ndim == 1:
        prepared = prepared.reshape(1, -1)

    faiss_ids = get_text_index().add(prepared)
    conn.executemany(
        """
        INSERT INTO embeddings (
            item_id,
            embedding_type,
            faiss_id,
            sparse_weights,
            model_name,
            created_at
        ) VALUES (?, ?, ?, NULL, ?, ?)
        """,
        [
            (item_id, embedding_type, faiss_id, config.EMBEDDING_MODEL, created_at)
            for faiss_id in faiss_ids
        ],
    )
    return faiss_ids


def _build_dense_inputs(
    normalized_text: str,
    chunk_source_text: str,
    *,
    chunked: bool,
    source_label: str = "",
) -> tuple[list[str], int]:
    if not chunked:
        dense_input = normalized_text or chunk_source_text
        return ([dense_input] if dense_input else []), (1 if dense_input else 0)

    chunks = chunk_text(chunk_source_text)
    if not chunks and chunk_source_text:
        chunks = [chunk_source_text]

    limited_chunks, original_chunk_count = _limit_document_chunks(
        chunks,
        source_label=source_label,
    )
    return limited_chunks, original_chunk_count


def _finalize_text_embeddings(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    sparse_json: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO embeddings (
            item_id,
            embedding_type,
            faiss_id,
            sparse_weights,
            model_name,
            created_at
        ) VALUES (?, 'sparse_json', NULL, ?, ?, ?)
        """,
        (item_id, sparse_json, config.EMBEDDING_MODEL, created_at),
    )
    conn.commit()


def embed_and_index_text(
    item_id: str,
    text: str,
    conn: sqlite3.Connection,
    chunked: bool = False,
    extra_text: str | None = None,
    *,
    source_label: str = "",
    summary_text: str | None = None,
) -> dict[str, Any]:
    normalized_text = (text or "").strip()
    chunk_source_text = (extra_text or normalized_text).strip()
    keyword_source = chunk_source_text or normalized_text
    keyword_weights, top_keywords = _extract_keywords(keyword_source)
    sparse_json = json.dumps(keyword_weights)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        DELETE FROM embeddings
        WHERE item_id = ?
          AND embedding_type IN ('dense', 'summary', 'sparse_json')
        """,
        (item_id,),
    )

    dense_inputs, chunk_count = _build_dense_inputs(
        normalized_text,
        chunk_source_text,
        chunked=chunked,
        source_label=source_label,
    )
    if not dense_inputs and normalized_text:
        dense_inputs = [normalized_text]
        chunk_count = max(chunk_count, 1)

    faiss_ids = _store_faiss_embeddings(
        conn,
        item_id,
        _embed_texts_sync(dense_inputs, source_label=source_label),
        embedding_type="dense",
        created_at=now,
    )

    summary_source = _document_summary_text(
        summary_text,
        normalized_text,
        chunk_source_text,
    )
    summary_faiss_id: int | None = None
    if summary_source:
        summary_ids = _store_faiss_embeddings(
            conn,
            item_id,
            _embed_texts_sync(
                [summary_source],
                source_label=f"{source_label} summary".strip(),
            ),
            embedding_type="summary",
            created_at=now,
        )
        if summary_ids:
            summary_faiss_id = summary_ids[0]

    _finalize_text_embeddings(
        conn,
        item_id,
        sparse_json=sparse_json,
        created_at=now,
    )

    return {
        "faiss_id": faiss_ids[0] if faiss_ids else summary_faiss_id,
        "faiss_ids": faiss_ids,
        "summary_faiss_id": summary_faiss_id,
        "chunk_count": chunk_count,
        "indexed_chunk_count": len(dense_inputs),
        "keywords": top_keywords,
    }


async def embed_and_index_text_async(
    item_id: str,
    text: str,
    conn: sqlite3.Connection,
    chunked: bool = False,
    extra_text: str | None = None,
    *,
    source_label: str = "",
    summary_text: str | None = None,
) -> dict[str, Any]:
    normalized_text = (text or "").strip()
    chunk_source_text = (extra_text or normalized_text).strip()
    keyword_source = chunk_source_text or normalized_text
    keyword_weights, top_keywords = _extract_keywords(keyword_source)
    sparse_json = json.dumps(keyword_weights)
    now = datetime.now(timezone.utc).isoformat()

    dense_inputs, chunk_count = _build_dense_inputs(
        normalized_text,
        chunk_source_text,
        chunked=chunked,
        source_label=source_label,
    )
    if not dense_inputs and normalized_text:
        dense_inputs = [normalized_text]
        chunk_count = max(chunk_count, 1)

    dense_vecs = np.empty((0, config.TEXT_EMBEDDING_DIM), dtype=np.float32)
    if dense_inputs:
        dense_vecs = await embed_texts_async(
            dense_inputs,
            source_label=source_label,
        )

    summary_source = _document_summary_text(
        summary_text,
        normalized_text,
        chunk_source_text,
    )
    summary_vecs = np.empty((0, config.TEXT_EMBEDDING_DIM), dtype=np.float32)
    if summary_source:
        summary_vecs = await embed_texts_async(
            [summary_source],
            source_label=f"{source_label} summary".strip(),
        )

    conn.execute(
        """
        DELETE FROM embeddings
        WHERE item_id = ?
          AND embedding_type IN ('dense', 'summary', 'sparse_json')
        """,
        (item_id,),
    )

    faiss_ids = _store_faiss_embeddings(
        conn,
        item_id,
        dense_vecs,
        embedding_type="dense",
        created_at=now,
    )
    summary_ids = _store_faiss_embeddings(
        conn,
        item_id,
        summary_vecs,
        embedding_type="summary",
        created_at=now,
    )
    summary_faiss_id = summary_ids[0] if summary_ids else None

    _finalize_text_embeddings(
        conn,
        item_id,
        sparse_json=sparse_json,
        created_at=now,
    )

    return {
        "faiss_id": faiss_ids[0] if faiss_ids else summary_faiss_id,
        "faiss_ids": faiss_ids,
        "summary_faiss_id": summary_faiss_id,
        "chunk_count": chunk_count,
        "indexed_chunk_count": len(dense_inputs),
        "keywords": top_keywords,
    }


async def embed_query_async(query: str) -> tuple[np.ndarray, dict]:
    loop = asyncio.get_running_loop()

    async with _query_model_pool.acquire() as model:
        vector = await loop.run_in_executor(
            None,
            partial(
                _encode_with_audit,
                model,
                _query_input(query),
                batch_size=1,
                source_label="query",
            ),
        )
    dense = _normalize_rows(vector)[0]
    return dense, {}


def embed_query(query: str) -> tuple[np.ndarray, dict]:
    with _query_model_pool.acquire_sync() as model:
        vector = _encode_with_audit(
            model,
            _query_input(query),
            batch_size=1,
            source_label="query",
        )
    dense = _normalize_rows(vector)[0]
    return dense, {}


def parse_sparse_weights(
    doc_sparse_json: "str | dict[str, Any] | None",
) -> "dict[str, Any] | None":
    """
    Parse a stored sparse_weights column (JSON string) into a dict once so the
    scoring loop does not pay json.loads() per candidate. Accepts either a raw
    JSON string or an already-parsed dict (idempotent).
    """
    if doc_sparse_json is None:
        return None
    if isinstance(doc_sparse_json, dict):
        return doc_sparse_json if doc_sparse_json else None
    try:
        parsed = json.loads(doc_sparse_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    return parsed


def compute_sparse_score(
    query_tokens: list[str],
    doc_sparse_json: "str | dict[str, Any] | None",
    *,
    normalized_query_tokens: "list[str] | None" = None,
) -> float:
    """
    Compute sparse similarity between query tokens and stored keyword weights.

    Callers in the hot path should pre-parse `doc_sparse_json` once via
    `parse_sparse_weights` and optionally pass a pre-lowercased, de-duplicated
    `normalized_query_tokens` list to avoid redoing that work per candidate.
    """
    if not doc_sparse_json or not query_tokens:
        return 0.0

    if isinstance(doc_sparse_json, dict):
        doc_weights: "dict[str, Any]" = doc_sparse_json
    else:
        parsed_weights = parse_sparse_weights(doc_sparse_json)
        if parsed_weights is None:
            return 0.0
        doc_weights = parsed_weights

    if not doc_weights:
        return 0.0

    if normalized_query_tokens is None:
        normalized_query_tokens = list(
            dict.fromkeys(token.lower() for token in query_tokens if token)
        )
    if not normalized_query_tokens:
        return 0.0

    total_weight = 0.0
    matched = 0

    for token_lower in normalized_query_tokens:
        if token_lower in doc_weights:
            total_weight += float(doc_weights[token_lower] or 0.0)
            matched += 1
            continue

        stemmed = token_lower.rstrip("s")
        if stemmed and stemmed in doc_weights:
            total_weight += float(doc_weights[stemmed] or 0.0) * 0.8
            matched += 1

    if matched == 0:
        return 0.0

    max_possible = sum(
        float(doc_weights.get(token_lower, 0.0) or 0.0)
        for token_lower in normalized_query_tokens
    ) or 1.0

    raw_score = total_weight / max_possible
    coverage = matched / len(normalized_query_tokens)
    return min(1.0, raw_score * coverage)
