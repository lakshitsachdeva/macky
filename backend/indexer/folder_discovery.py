from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from .. import database
except ImportError:
    import database


HOME = Path.home()
NEVER_WATCH = [
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/var",
    str(HOME / "Library"),
    str(HOME / ".Trash"),
]
INDEXABLE_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".txt",
    ".md",
    ".rtf",
    ".csv",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".mp4",
    ".mov",
    ".zip",
    ".py",
    ".js",
    ".ts",
    ".swift",
    ".html",
    ".css",
}

_CACHE_TTL_SECONDS = 30.0
_cache_expires_at = 0.0
_cached_result: dict[str, Any] | None = None


def discover_folders() -> dict[str, list[dict[str, Any]]]:
    """
    Returns a structured dict of discoverable folder locations on this Mac.
    """
    result: dict[str, list[dict[str, Any]]] = {
        "standard": [],
        "icloud": [],
        "external": [],
        "cloud": [],
        "suggested": [],
        "custom": [],
    }

    watched_paths = _get_watched_paths()

    standard_folders = [
        ("~/Downloads", "Downloads"),
        ("~/Desktop", "Desktop"),
        ("~/Documents", "Documents"),
        ("~/Pictures", "Pictures"),
        ("~/Movies", "Movies"),
        ("~/Music", "Music"),
        ("~/Developer", "Developer"),
    ]
    for raw_path, label in standard_folders:
        path = Path(raw_path).expanduser()
        if path.exists():
            result["standard"].append(
                {
                    "path": str(path),
                    "label": label,
                    "file_count": _count_indexable_files(path, max_depth=2),
                    "is_watched": str(path) in watched_paths,
                }
            )

    icloud_base = HOME / "Library" / "Mobile Documents"
    if icloud_base.exists():
        icloud_drive = icloud_base / "com~apple~CloudDocs"
        if icloud_drive.exists():
            result["icloud"].append(
                {
                    "path": str(icloud_drive),
                    "label": "iCloud Drive",
                    "file_count": _count_indexable_files(icloud_drive, max_depth=2),
                    "is_watched": str(icloud_drive) in watched_paths,
                }
            )

        for app_dir in icloud_base.iterdir():
            if not app_dir.is_dir() or "~apple~" not in app_dir.name:
                continue

            docs_dir = app_dir / "Documents"
            target = docs_dir if docs_dir.exists() else app_dir
            count = _count_indexable_files(target, max_depth=1)
            if count <= 0:
                continue

            app_name = app_dir.name.split("~")[-1].replace("~", " ").title()
            result["icloud"].append(
                {
                    "path": str(target),
                    "label": f"iCloud {app_name}",
                    "file_count": count,
                    "is_watched": str(target) in watched_paths,
                }
            )

    volumes = Path("/Volumes")
    if volumes.exists():
        for volume in volumes.iterdir():
            if not volume.is_dir() or volume.is_symlink():
                continue
            if volume.name in {"Macintosh HD", "Macintosh HD - Data"}:
                continue

            count = _count_indexable_files(volume, max_depth=2)
            result["external"].append(
                {
                    "path": str(volume),
                    "label": volume.name,
                    "file_count": count,
                    "is_network": _is_network_drive(volume),
                    "is_external": True,
                    "is_watched": str(volume) in watched_paths,
                }
            )

    cloud_paths: list[tuple[Path, str | None]] = [
        (HOME / "Dropbox", "Dropbox"),
        (HOME / "Google Drive", "Google Drive"),
        (HOME / "OneDrive", "OneDrive"),
        (HOME / "Box", "Box"),
        (HOME / "Nextcloud", "Nextcloud"),
        (HOME / "Library" / "CloudStorage", None),
    ]
    for cloud_path, label in cloud_paths:
        if not cloud_path.exists():
            continue

        if label is None:
            for provider_dir in cloud_path.iterdir():
                if not provider_dir.is_dir():
                    continue
                result["cloud"].append(
                    {
                        "path": str(provider_dir),
                        "label": _prettify_cloud_name(provider_dir.name),
                        "file_count": _count_indexable_files(provider_dir, max_depth=2),
                        "is_watched": str(provider_dir) in watched_paths,
                    }
                )
        else:
            result["cloud"].append(
                {
                    "path": str(cloud_path),
                    "label": label,
                    "file_count": _count_indexable_files(cloud_path, max_depth=2),
                    "is_watched": str(cloud_path) in watched_paths,
                }
            )

    result["suggested"] = _find_recent_hotspots(watched_paths)
    result["custom"] = _get_custom_watched_folders()
    return result


def get_cached_discovery() -> dict[str, list[dict[str, Any]]]:
    global _cache_expires_at, _cached_result

    now = time.monotonic()
    if _cached_result is not None and now < _cache_expires_at:
        return _cached_result

    _cached_result = discover_folders()
    _cache_expires_at = now + _CACHE_TTL_SECONDS
    return _cached_result


def invalidate_discovery_cache() -> None:
    global _cache_expires_at, _cached_result
    _cache_expires_at = 0.0
    _cached_result = None


def _count_indexable_files(path: Path, max_depth: int = 2) -> int:
    count = 0
    for item in _walk_limited(path, max_depth):
        try:
            if item.is_file() and item.suffix.lower() in INDEXABLE_EXTENSIONS:
                count += 1
        except OSError:
            continue

        if count >= 9999:
            return 9999
    return count


def _walk_limited(path: Path, max_depth: int):
    def _walk(current: Path, depth: int):
        if depth > max_depth:
            return
        try:
            for item in current.iterdir():
                if item.name.startswith("."):
                    continue
                yield item
                if item.is_dir():
                    yield from _walk(item, depth + 1)
        except (PermissionError, OSError):
            return

    yield from _walk(path, 0)


def _is_network_drive(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["mount"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        path_str = str(path)
        for line in result.stdout.splitlines():
            if path_str not in line:
                continue
            if any(fs in line for fs in ("nfs", "smb", "afp", "cifs", "smbfs")):
                return True
    except Exception:
        return False
    return False


def _find_recent_hotspots(watched_paths: set[str]) -> list[dict[str, Any]]:
    hotspots: dict[str, int] = {}
    skip_prefixes = [
        *NEVER_WATCH,
        str(HOME / "Downloads"),
        str(HOME / "Desktop"),
        str(HOME / "Documents"),
        str(HOME / "Pictures"),
        str(HOME / "Movies"),
        str(HOME / "Music"),
        str(HOME / "Developer"),
        "/private",
    ]

    try:
        result = subprocess.run(
            [
                "mdfind",
                "-onlyin",
                str(HOME),
                "kMDItemFSContentChangeDate >= $time.today(-14)",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return []

    for filepath in result.stdout.splitlines()[:500]:
        path = Path(filepath)
        if path.name.startswith("."):
            continue

        parent = str(path.parent)
        if any(parent.startswith(skip) for skip in skip_prefixes):
            continue
        if parent in watched_paths:
            continue
        if any(parent.startswith(blocked) for blocked in NEVER_WATCH):
            continue

        hotspots[parent] = hotspots.get(parent, 0) + 1

    suggestions = [
        {
            "path": folder_path,
            "label": Path(folder_path).name or folder_path,
            "file_count": count,
            "is_watched": False,
        }
        for folder_path, count in hotspots.items()
        if count >= 3
    ]
    suggestions.sort(key=lambda item: item["file_count"], reverse=True)
    return suggestions[:8]


def _prettify_cloud_name(name: str) -> str:
    replacements = {
        "googledrive": "Google Drive",
        "onedrive": "OneDrive",
        "dropbox": "Dropbox",
        "box": "Box",
        "nextcloud": "Nextcloud",
    }
    lowered = name.lower()
    for key, value in replacements.items():
        if key in lowered:
            return value
    return name.replace("-", " ").replace("_", " ").title()


def _get_watched_paths() -> set[str]:
    conn = database.get_connection()
    try:
        rows = database.get_watched_folders(conn)
    finally:
        conn.close()
    return {str(row["path"]) for row in rows}


def _get_custom_watched_folders() -> list[dict[str, Any]]:
    conn = database.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT path, label, added_at, is_default
            FROM watched_folders
            WHERE COALESCE(is_default, 0) = 0
            ORDER BY added_at, label
            """
        ).fetchall()
    finally:
        conn.close()

    custom: list[dict[str, Any]] = []
    for row in rows:
        path = Path(str(row["path"]))
        custom.append(
            {
                "path": str(path),
                "label": str(row["label"]),
                "file_count": _count_indexable_files(path, max_depth=2)
                if path.exists()
                else 0,
                "is_watched": True,
            }
        )
    return custom
