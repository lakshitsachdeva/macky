from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parent
TMPDIR = Path(tempfile.mkdtemp())

os.environ["RECURDO_DATA_DIR"] = str(TMPDIR / "recurdo_data")
os.environ["RECURDO_ENV_FILE"] = str(TMPDIR / "test.env")
os.environ["DB_PATH"] = str(TMPDIR / "memory.db")
os.environ["TEXT_FAISS_PATH"] = str(TMPDIR / "text_faiss.index")
os.environ["WATCHED_DIRS"] = str(TMPDIR)
os.environ["GEMINI_API_KEY"] = ""

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import database
from backend.indexer import pipeline, visual_embedder
from backend.retrieval import search


def _load_font(size: int) -> ImageFont.ImageFont:
    for candidate in ("DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _write_iit_notice_image(path: Path) -> None:
    image = Image.new("RGB", (1400, 1600), "#fff4f1")
    draw = ImageDraw.Draw(image)
    font = _load_font(54)
    draw.multiline_text(
        (70, 80),
        (
            "INDIAN INSTITUTE OF TECHNOLOGY MADRAS\n"
            "CHENNAI 600036\n\n"
            "Summer Fellowship Programme 2026\n\n"
            "Internship opportunity for UG and PG students\n"
            "of other institutions.\n\n"
            "Apply at https://ssp.iitm.ac.in\n"
            "IIT Madras Research Programme Notice"
        ),
        fill="black",
        spacing=24,
        font=font,
    )
    image.save(path, format="PNG")


def main() -> int:
    try:
        print(f"Using temp workspace: {TMPDIR}")
        print(f"Tesseract available: {visual_embedder.HAS_TESSERACT}")
        print(f"Gemini vision available: {visual_embedder.HAS_GEMINI_VISION}")

        if not (visual_embedder.HAS_TESSERACT or visual_embedder.HAS_GEMINI_VISION):
            print("SKIP: no OCR or vision backend available")
            return 0

        database.init_db()

        image_path = TMPDIR / "iit_madras_notice.png"
        _write_iit_notice_image(image_path)

        result = asyncio.run(pipeline.index_file(image_path))
        print(f"Index result: {result}")
        if result.get("status") not in {"indexed", "duplicate"}:
            print("FAIL: image did not index successfully")
            return 1

        results = asyncio.run(search.search("iit madras", limit=5))
        top_filenames = [hit.item["filename"] for hit in results[:3]]
        print(f"Top filenames: {top_filenames}")

        if "iit_madras_notice.png" not in top_filenames:
            print("FAIL: expected OCR-heavy image in top 3 for query 'iit madras'")
            return 1

        top = results[0].item["filename"] if results else "no results"
        score = results[0].final_score if results else 0.0
        print(f"PASS: got {top} ({score:.2f}) and target is in top 3")
        return 0
    finally:
        shutil.rmtree(TMPDIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
