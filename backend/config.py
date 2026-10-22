from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

from dotenv import load_dotenv


def _has_optional_module(module_name: str) -> bool:
    try:
        return find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return False


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = Path.home() / ".recurdo"
RECURDO_ENV = os.environ.get("RECURDO_ENV", "production").strip().lower() or "production"


def _resolve_base_path(raw_value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = (base or PROJECT_ROOT) / path
    return path


def _load_config() -> Path:
    data_dir = _resolve_base_path(
        os.environ.get("RECURDO_DATA_DIR", str(DEFAULT_DATA_DIR)),
        base=DEFAULT_DATA_DIR.parent,
    )
    data_dir.mkdir(parents=True, exist_ok=True)

    user_env = _resolve_base_path(
        os.environ.get("RECURDO_ENV_FILE", str(data_dir / ".env")),
        base=data_dir,
    )
    if user_env.exists():
        load_dotenv(user_env, override=False)

    override_env = data_dir / "backend.env"
    if override_env.exists():
        load_dotenv(override_env, override=True)

    if RECURDO_ENV == "development":
        repo_env = PROJECT_ROOT / ".env"
        if repo_env.exists():
            load_dotenv(repo_env, override=False)
            print(f"[Config] Dev mode: loaded repo .env from {repo_env}")

    return data_dir


DATA_DIR = _load_config()
USER_RECURDO_DIR = DATA_DIR
USER_BACKEND_ENV_PATH = DATA_DIR / "backend.env"
ENV_FILE = _resolve_base_path(
    os.environ.get("RECURDO_ENV_FILE", str(DATA_DIR / ".env")),
    base=DATA_DIR,
)


def _default_storage_root() -> Path:
    if getattr(sys, "frozen", False):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _path_from_env(names: tuple[str, ...], default: str) -> Path:
    value = ""
    for name in names:
        raw = os.getenv(name, "").strip()
        if raw:
            value = raw
            break
    if not value:
        value = default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _default_storage_root() / path
    return path


def _parse_watched_dirs(value: str) -> list[Path]:
    watched_dirs: list[Path] = []
    base_dir = PROJECT_ROOT if RECURDO_ENV == "development" else DATA_DIR

    for raw_dir in value.split(","):
        raw_dir = raw_dir.strip()
        if not raw_dir:
            continue

        directory = Path(raw_dir).expanduser()
        if not directory.is_absolute():
            directory = base_dir / directory

        if directory.is_dir():
            watched_dirs.append(directory)

    return watched_dirs


def _parse_excluded_dirs(value: str) -> list[Path]:
    """Parse comma-separated excluded directory paths (need not exist yet)."""
    excluded: list[Path] = []
    for raw_dir in value.split(","):
        raw_dir = raw_dir.strip()
        if not raw_dir:
            continue
        excluded.append(Path(raw_dir).expanduser().resolve())
    return excluded


def _detect_tesseract() -> str | None:
    candidates: list[str] = []

    for env_name in ("TESSERACT_CMD", "TESSERACT_PATH"):
        raw_value = os.getenv(env_name, "").strip()
        if raw_value:
            candidates.append(raw_value)

    which_value = shutil.which("tesseract")
    if which_value:
        candidates.append(which_value)

    candidates.extend(
        [
            "/opt/homebrew/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/usr/bin/tesseract",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        expanded = str(Path(candidate).expanduser())
        if expanded in seen:
            continue
        seen.add(expanded)

        try:
            subprocess.run(
                [expanded, "--version"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue
        return expanded

    return None


WATCHED_DIRS = _parse_watched_dirs(os.getenv("WATCHED_DIRS", ""))
EXCLUDED_DIRS: list[Path] = _parse_excluded_dirs(os.getenv("EXCLUDED_DIRS", ""))

DB_PATH = _path_from_env(("DB_PATH",), "memory.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

TEXT_FAISS_PATH = _path_from_env(("TEXT_FAISS_PATH", "FAISS_PATH"), "index.faiss")
TEXT_FAISS_PATH.parent.mkdir(parents=True, exist_ok=True)

VECTORS_DB_PATH = _path_from_env(("VECTORS_DB_PATH", "VECTOR_DB_PATH"), "vectors.db")
VECTORS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

GRAPH_DB_PATH = _path_from_env(("GRAPH_DB_PATH",), "graph.db")
GRAPH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
GRAPH_STORE_BACKEND = os.getenv("GRAPH_STORE_BACKEND", "sqlite").strip().lower() or "sqlite"
GRAPH_STORE_STRICT = os.getenv("GRAPH_STORE_STRICT", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FALKOR_GRAPH_NAME = os.getenv("FALKOR_GRAPH_NAME", "recurdo_memory").strip() or "recurdo_memory"

COGNITIVE_DB_PATH = _path_from_env(("COGNITIVE_DB_PATH",), "cognitive_layer.db")
COGNITIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

ATTACHMENTS_DIR = _path_from_env(("ATTACHMENTS_DIR", "ATTACHMENTS"), "attachments")
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

GMAIL_TOKENS_DIR = _path_from_env(
    ("GMAIL_TOKENS_DIR", "GMAIL_TOKENS"),
    "gmail_tokens",
)
GMAIL_TOKENS_DIR.mkdir(parents=True, exist_ok=True)

AUTH_CACHE_PATH = _path_from_env(("AUTH_CACHE_PATH",), "auth_cache.json")
AUTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

def _bundled_resource_root() -> Path:
    raw_meipass = getattr(sys, "_MEIPASS", None)
    if raw_meipass:
        return Path(str(raw_meipass))
    return Path(__file__).resolve().parent


def _default_embedding_model() -> str:
    bundled = _bundled_resource_root() / "models" / "nomic-embed-text-v1.5"
    if bundled.exists():
        return str(bundled)
    return "nomic-ai/nomic-embed-text-v1.5"


EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", _default_embedding_model())
BGE_MODEL_NAME = EMBEDDING_MODEL
TEXT_EMBEDDING_DIM = 768
# Gemini 2.0 Flash was shut down. Keep the default on a currently served
# structured-output-capable Flash endpoint; operators can override this in the
# ignored data/backend.env file without changing source code.
ENRICHMENT_MODEL = os.getenv("ENRICHMENT_MODEL", "gemini-3.8-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
HAS_GEMINI_SDK = _has_optional_module("google.generativeai")
# Gemini vision sends image bytes to Google — off by default for privacy.
# Vision is feature-flagged during private beta. Set RECURDO_ENABLE_GEMINI_VISION=1
# to allow enabling it.
GEMINI_VISION_ENABLED: bool = os.getenv("GEMINI_VISION_ENABLED", "false").lower() in ("1", "true", "yes")
RECURDO_ENABLE_GEMINI_VISION: bool = os.getenv("RECURDO_ENABLE_GEMINI_VISION", "").lower() in ("1", "true", "yes")
if not RECURDO_ENABLE_GEMINI_VISION:
    GEMINI_VISION_ENABLED = False
TESSERACT_CMD = _detect_tesseract()
HAS_TESSERACT = TESSERACT_CMD is not None
PORT = int(os.getenv("RECURDO_PORT", os.getenv("PORT", "8000")))

SUPPORTED_TEXT_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".txt": "text",
    ".md": "text",
    ".csv": "text",
    ".xlsx": "spreadsheet",
    ".pptx": "presentation",
}

SUPPORTED_IMAGE_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
}

SUPPORTED_EXTENSIONS: set[str] = (
    set(SUPPORTED_TEXT_EXTENSIONS) | SUPPORTED_IMAGE_EXTENSIONS
)

MAX_FILE_SIZE_MB = 100
MAX_FILE_AGE_DAYS = 180
SESSION_GAP_MINUTES = 30
