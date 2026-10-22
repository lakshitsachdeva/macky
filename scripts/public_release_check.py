#!/usr/bin/env python3
"""Fail closed on common public-release mistakes.

This check intentionally reports only paths/pattern names, never secret values.
It is a release hygiene check, not a complete secret scanner.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yml",
    ".yaml",
}

PRIVATE_NAMES = {".env", ".env.local"}
PRIVATE_DIRS = {
    ".git",
    ".pytest_cache",
    ".run",
    ".venv-custody",
    "backend/dist",
    "backend/models",
    "build",
    "data",
    "dist",
    "frontend/node_modules",
    "node_modules",
    "venv311",
}

SECRET_PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    "JWT-like token": re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    "private service key": re.compile(r"(?i)service[_-]?role\s*[:=]\s*[^\s#]+"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

PRIVATE_PATH_RE = re.compile(r"/Users/[^/\s]+")


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        relative_text = relative.as_posix()
        if any(part in {".git", ".pytest_cache", "__pycache__"} for part in relative.parts):
            continue
        if any(relative_text == directory or relative_text.startswith(f"{directory}/") for directory in PRIVATE_DIRS):
            continue
        if path.name in PRIVATE_NAMES:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def tracked_files() -> set[str] | None:
    """Return tracked paths when Git is usable, otherwise use the workspace scan."""
    try:
        output = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return {entry.decode("utf-8") for entry in output.split(b"\0") if entry}


def main() -> int:
    failures: list[str] = []
    tracked = tracked_files()
    for path in iter_files():
        relative = path.relative_to(ROOT).as_posix()
        if tracked is not None and relative not in tracked:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{label} pattern found in {relative}")
        if PRIVATE_PATH_RE.search(text) and relative.startswith("docs/"):
            failures.append(f"absolute personal path found in public docs: {relative}")

    if failures:
        print("PUBLIC RELEASE CHECK FAILED")
        for failure in sorted(set(failures)):
            print(f"- {failure}")
        return 1

    print("PUBLIC RELEASE CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
