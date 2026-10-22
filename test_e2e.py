from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


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
from backend.indexer import embedder, pipeline, visual_embedder
from backend.retrieval import search


def _write_image(path: Path) -> None:
    image = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.multiline_text(
        (36, 48),
        (
            "ERROR: ConnectionRefusedError\n"
            "Port 8080 is not responding\n"
            "Check firewall settings and retry"
        ),
        fill="black",
        spacing=10,
    )
    image.save(path, format="PNG")


def _create_test_files(base_dir: Path) -> list[Path]:
    files = {
        "ml_paper.txt": (
            "Deep learning with transformer architectures enables large language\n"
            "models to perform few-shot reasoning. Attention mechanisms allow the\n"
            "model to focus on relevant context across long sequences."
        ),
        "invoice.txt": (
            "INVOICE #2024-089\n"
            "From: Acme Consulting Ltd\n"
            "To: Client Corp\n"
            "Services: Software development consulting\n"
            "Amount Due: $4,500.00\n"
            "Due: 30 days"
        ),
        "meeting_notes.md": (
            "# Q4 Strategy Meeting Notes\n"
            "- Budget review: $2M approved\n"
            "- Product roadmap: launch in March\n"
            "- Team: hiring 5 engineers\n"
            "- Action items: complete by end of quarter"
        ),
        "sales_data.csv": (
            "month,revenue,customers,churn_rate\n"
            "January,45000,120,0.03\n"
            "February,52000,145,0.02\n"
            "March,61000,178,0.025\n"
            "April,58000,165,0.03"
        ),
    }

    paths: list[Path] = []
    for filename, content in files.items():
        path = base_dir / filename
        path.write_text(content, encoding="utf-8")
        paths.append(path)

    image_path = base_dir / "error_screenshot.png"
    _write_image(image_path)
    paths.append(image_path)
    return paths


def _index_files(paths: list[Path]) -> None:
    for path in paths:
        result = asyncio.run(pipeline.index_file(path))
        detail = result.get("reason") or result.get("title") or result.get("error") or ""
        print(f"{path.name}: {result['status']}{f' ({detail})' if detail else ''}")


def _check_embedding() -> bool:
    try:
        vector = embedder.embed_text("test")
    except Exception as exc:
        print(f"❌ Embedding broken ({exc})")
        return False

    if tuple(getattr(vector, "shape", ())) == (768,):
        print("✅ Nomic embed working (768-dim)")
        return True

    print(f"❌ Embedding broken (shape={getattr(vector, 'shape', None)})")
    return False


def _run_search_checks() -> tuple[int, str]:
    text_tests = [
        ("transformer attention language model", "ml_paper.txt", "ML paper"),
        ("invoice Acme consulting amount due", "invoice.txt", "Invoice"),
        ("Q4 strategy budget meeting notes", "meeting_notes.md", "Meeting notes"),
        ("monthly revenue sales data customers", "sales_data.csv", "Sales CSV"),
    ]
    image_test = (
        "connection refused error port 8080",
        "error_screenshot.png",
        "Screenshot",
    )
    text_passed = 0
    for query, expected_filename, label in text_tests:
        try:
            results = asyncio.run(search.search(query, limit=5))
        except Exception as exc:
            print(f"❌ {label}: expected {expected_filename}, search crashed ({exc})")
            continue

        top_filenames = [result.item["filename"] for result in results[:3]]
        ok = expected_filename in top_filenames
        text_passed += int(ok)
        status = "✅" if ok else "❌"
        top = results[0].item["filename"] if results else "no results"
        score = results[0].final_score if results else 0.0
        print(
            f"{status} {label}: expected {expected_filename}, got {top} ({score:.2f})"
        )

    image_status = "skipped"
    if visual_embedder.HAS_TESSERACT or visual_embedder.HAS_GEMINI_VISION:
        query, expected_filename, label = image_test
        try:
            results = asyncio.run(search.search(query, limit=5))
        except Exception as exc:
            image_status = "failed"
            print(f"❌ {label}: expected {expected_filename}, search crashed ({exc})")
        else:
            top_filenames = [result.item["filename"] for result in results[:3]]
            ok = expected_filename in top_filenames
            image_status = "passed" if ok else "failed"
            status = "✅" if ok else "❌"
            top = results[0].item["filename"] if results else "no results"
            score = results[0].final_score if results else 0.0
            print(
                f"{status} {label}: expected {expected_filename}, got {top} ({score:.2f})"
            )
    else:
        print("⚠️  Screenshot (skipped — no OCR or vision available)")

    overall_pass = text_passed == len(text_tests)

    print(f"\n{'=' * 40}")
    print(f"Text search:  {text_passed}/{len(text_tests)} passed")
    if image_status == "passed":
        print("Image search: 1/1 passed")
    elif image_status == "failed":
        print("Image search: 0/1 failed")
    else:
        print("Image search: skipped")
    print(f"Overall: {'PASS' if overall_pass else 'FAIL'}")

    return text_passed, image_status


def main() -> int:
    try:
        print(f"Using temp workspace: {TMPDIR}")
        print(f"Tesseract available: {visual_embedder.HAS_TESSERACT}")
        print(f"Gemini vision available: {visual_embedder.HAS_GEMINI_VISION}")

        if not _check_embedding():
            return 1

        database.init_db()
        paths = _create_test_files(TMPDIR)

        print("\nIndexing files")
        print("=" * 40)
        _index_files(paths)

        print("\nSearch checks")
        print("=" * 40)
        text_passed, _ = _run_search_checks()
        return 0 if text_passed == 4 else 1
    finally:
        shutil.rmtree(TMPDIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
