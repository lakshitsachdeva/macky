from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download


MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
REMOTE_CODE_MODEL_ID = "nomic-ai/nomic-bert-2048"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_MODEL_DIR = PROJECT_ROOT / "backend" / "models" / "nomic-embed-text-v1.5"
REMOTE_CODE_FILES = (
    "configuration_hf_nomic_bert.py",
    "modeling_hf_nomic_bert.py",
)


def _dir_size_mb(path: Path) -> float:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) / 1e6


def _download_remote_code_files() -> None:
    print(f"Downloading Nomic remote-code shims from {REMOTE_CODE_MODEL_ID}...")
    for filename in REMOTE_CODE_FILES:
        hf_hub_download(
            repo_id=REMOTE_CODE_MODEL_ID,
            filename=filename,
            local_dir=str(LOCAL_MODEL_DIR),
            local_dir_use_symlinks=False,
        )


def _rewrite_auto_map_to_local_paths() -> None:
    config_path = LOCAL_MODEL_DIR / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Model config not found at {config_path}")

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    auto_map = payload.get("auto_map")
    if not isinstance(auto_map, dict):
        return

    changed = False
    for key, value in list(auto_map.items()):
        if not isinstance(value, str):
            continue
        if "--" in value:
            _, local_ref = value.split("--", 1)
            auto_map[key] = local_ref
            changed = True

    if changed:
        config_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        print(f"Rewrote auto_map entries to local module refs in {config_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete and redownload the base embedding model before patching.",
    )
    args = parser.parse_args()

    if LOCAL_MODEL_DIR.exists() and args.fresh:
        print(f"Removing existing bundled model at {LOCAL_MODEL_DIR}...")
        shutil.rmtree(LOCAL_MODEL_DIR)

    LOCAL_MODEL_DIR.mkdir(parents=True, exist_ok=True)

    if not (LOCAL_MODEL_DIR / "config.json").exists():
        print(f"Downloading {MODEL_ID} to {LOCAL_MODEL_DIR}...")
        snapshot_download(
            repo_id=MODEL_ID,
            local_dir=str(LOCAL_MODEL_DIR),
            local_dir_use_symlinks=False,
            ignore_patterns=["*.msgpack", "flax_model*", "tf_model*", "rust_model*"],
        )
    else:
        print(f"Reusing existing bundled model at {LOCAL_MODEL_DIR}")

    _download_remote_code_files()
    _rewrite_auto_map_to_local_paths()

    print(f"Bundled model ready at {LOCAL_MODEL_DIR}")
    print(f"Size: {_dir_size_mb(LOCAL_MODEL_DIR):.0f} MB")
    print("Next: run backend/build_backend.sh to bundle the packaged backend.")


if __name__ == "__main__":
    main()
