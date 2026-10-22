from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

try:
    from . import config
except ImportError:
    import config


HASH_CHUNK_SIZE = 1024 * 1024
SKIP_DIR_PARTS = {
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


def hash_file_streaming(
    path: str | Path,
    *,
    algorithm: str = "sha256",
    chunk_size: int = HASH_CHUNK_SIZE,
) -> str:
    """Hash a source file without loading its contents into memory."""
    if algorithm.casefold() != "sha256":
        raise ValueError("Only sha256 is supported")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _supported_file(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() in config.SUPPORTED_EXTENSIONS


def discover_supported_files(paths: Iterable[str | Path]) -> list[Path]:
    """Recursively discover indexable files while tolerating inaccessible paths."""
    discovered: dict[str, Path] = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            resolved = path

        try:
            if resolved.is_file():
                if _supported_file(resolved):
                    discovered[str(resolved)] = resolved
                continue
            if not resolved.is_dir():
                continue
        except OSError:
            continue

        for root, dirs, filenames in os.walk(resolved, onerror=lambda _error: None):
            dirs[:] = [
                name
                for name in dirs
                if (
                    not name.startswith(".")
                    and name not in SKIP_DIR_PARTS
                    and not any(
                        name.casefold().endswith(suffix)
                        for suffix in SKIP_DIR_SUFFIXES
                    )
                )
            ]
            root_path = Path(root)
            if any(part in SKIP_DIR_PARTS for part in root_path.parts):
                continue
            for filename in filenames:
                if filename.startswith("."):
                    continue
                candidate = root_path / filename
                try:
                    if _supported_file(candidate):
                        discovered[str(candidate.resolve())] = candidate.resolve()
                except (OSError, PermissionError):
                    continue

    return sorted(discovered.values(), key=lambda path: str(path).casefold())
