"""
Unit tests for the conversational /ask synthesis helpers.

The goal is to pin the behavior users actually see in the UI:
  - Source blocks render cleanly from whatever shape _serialize_search_results
    produces (including missing fields, long snippets, Gmail-style items).
  - Confidence inference doesn't over-promise: a hedged answer reads as
    'insufficient' even if the model dropped a citation, and a zero-result
    retrieval is always 'insufficient'.
  - The fallback path always returns a non-empty string — the endpoint
    contract is "never 500 because the model is offline".

These tests import the synthesis module in isolation. They do NOT boot FastAPI,
touch SQLite, or call an LLM, so they run in under a second and can live on
the main smoke-test path.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Stub sentence_transformers before importing anything that pulls in the
# embedder — the CI sandbox does not have torch/sentence-transformers, and
# these helpers don't need them.
if "sentence_transformers" not in sys.modules:
    stub = types.ModuleType("sentence_transformers")
    class _StubModel:  # pragma: no cover — only used to satisfy imports
        def __init__(self, *_args, **_kwargs) -> None: ...
        def encode(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return []
    stub.SentenceTransformer = _StubModel  # type: ignore[attr-defined]
    sys.modules["sentence_transformers"] = stub

_TMP = Path(tempfile.mkdtemp())
os.environ.setdefault("RECURDO_DATA_DIR", str(_TMP / "recurdo_data"))
os.environ.setdefault("RECURDO_ENV_FILE", str(_TMP / "test.env"))
os.environ.setdefault("DB_PATH", str(_TMP / "ask.db"))
os.environ.setdefault("TEXT_FAISS_PATH", str(_TMP / "text_faiss.index"))
os.environ.setdefault("WATCHED_DIRS", str(_TMP))
os.environ.setdefault("GEMINI_API_KEY", "")


from backend.llm import synthesis  # noqa: E402


def _item(**overrides):
    base = {
        "id": "item-1",
        "title": "Recurdo Bible",
        "filename": "recurdo-bible.md",
        "source_type": "markdown",
        "modified_at": "2026-04-15T12:00:00Z",
        "preview_text": "This is the canonical document capturing Recurdo's architecture and goals.",
        "final_score": 1.23,
    }
    base.update(overrides)
    return base


class RenderAskSourcesTests(unittest.TestCase):
    def test_numbered_block_includes_title_and_metadata(self) -> None:
        block = synthesis._render_ask_sources([_item()], limit=6)
        self.assertIn("[1] Recurdo Bible", block)
        self.assertIn("recurdo-bible.md", block)
        self.assertIn("markdown", block)
        # snippet should be indented for readability
        self.assertIn("    This is the canonical document", block)

    def test_long_snippets_are_clipped(self) -> None:
        long_preview = "x" * 1000
        block = synthesis._render_ask_sources([_item(preview_text=long_preview)], limit=6)
        # Clip budget is 320 chars; the indented snippet line should be
        # strictly shorter than the full preview.
        snippet_lines = [ln for ln in block.splitlines() if ln.startswith("    ")]
        self.assertTrue(snippet_lines, "expected at least one snippet line")
        self.assertLess(len(snippet_lines[0]), len(long_preview))

    def test_empty_results_returns_placeholder(self) -> None:
        self.assertEqual(
            synthesis._render_ask_sources([], limit=6),
            "(no sources retrieved)",
        )

    def test_limit_truncates_sources(self) -> None:
        items = [_item(id=f"id-{i}", title=f"Item {i}") for i in range(10)]
        block = synthesis._render_ask_sources(items, limit=3)
        self.assertIn("[1] Item 0", block)
        self.assertIn("[3] Item 2", block)
        self.assertNotIn("[4]", block)


class InferConfidenceTests(unittest.TestCase):
    def test_no_results_is_insufficient(self) -> None:
        self.assertEqual(
            synthesis._infer_answer_confidence("Any answer.", []),
            "insufficient",
        )

    def test_hedge_is_insufficient(self) -> None:
        for hedge in (
            "I don't have enough in your library to answer this.",
            "I do not have enough context.",
            "I cannot answer that from your files.",
            "I can't answer based on what's here.",
        ):
            self.assertEqual(
                synthesis._infer_answer_confidence(hedge, [_item()]),
                "insufficient",
                msg=f"hedge not detected: {hedge!r}",
            )

    def test_citation_count_scales_confidence(self) -> None:
        results = [_item(), _item(id="item-2")]
        self.assertEqual(
            synthesis._infer_answer_confidence("Fact A [1]. Fact B [2].", results),
            "high",
        )
        self.assertEqual(
            synthesis._infer_answer_confidence("One thing [1].", results),
            "medium",
        )
        self.assertEqual(
            synthesis._infer_answer_confidence("No brackets at all.", results),
            "low",
        )

    def test_duplicate_citations_collapsed(self) -> None:
        # [1] cited twice should still only count as one distinct source.
        results = [_item()]
        self.assertEqual(
            synthesis._infer_answer_confidence("Fact A [1]. Also [1].", results),
            "medium",
        )


class FallbackAnswerTests(unittest.TestCase):
    def test_zero_results_returns_no_matches_message(self) -> None:
        text = synthesis.build_ask_fallback_answer("coffee with Priya", [])
        self.assertIn("coffee with Priya", text)
        self.assertIn("do not have matches", text)

    def test_single_result_cites_once(self) -> None:
        text = synthesis.build_ask_fallback_answer("what", [_item()])
        self.assertIn("[1]", text)
        self.assertNotIn("[2]", text)

    def test_three_results_all_cited(self) -> None:
        items = [
            _item(id="a", title="Alpha"),
            _item(id="b", title="Beta"),
            _item(id="c", title="Gamma"),
        ]
        text = synthesis.build_ask_fallback_answer("q", items)
        for tag in ("[1]", "[2]", "[3]"):
            self.assertIn(tag, text)


class SynthesizeAskAnswerTests(unittest.TestCase):
    """synthesize_ask_answer should NEVER 500. If the LLM is unavailable or
    throws, it must degrade gracefully to the heuristic fallback."""

    def test_llm_unavailable_falls_back(self) -> None:
        class _UnavailableLLM:
            def status(self):
                return {"available": False, "provider": "none"}
            async def generate(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("should not be called when unavailable")

        out = asyncio.run(
            synthesis.synthesize_ask_answer(
                "what did I decide about latency?",
                [_item()],
                llm=_UnavailableLLM(),
            )
        )
        self.assertEqual(out["provider"], "heuristic")
        self.assertIn(out["confidence"], {"low", "insufficient"})
        self.assertTrue(out["text"])

    def test_llm_exception_falls_back_not_raises(self) -> None:
        class _FlakyLLM:
            def status(self):
                return {"available": True, "provider": "qwen3-4b-local"}
            async def generate(self, *args, **kwargs):
                raise RuntimeError("MPS out of memory")

        out = asyncio.run(
            synthesis.synthesize_ask_answer(
                "anything?",
                [_item()],
                llm=_FlakyLLM(),
            )
        )
        self.assertEqual(out["provider"], "heuristic")
        self.assertTrue(out["text"])

    def test_llm_success_path_returns_model_text(self) -> None:
        class _FakeLLM:
            def status(self):
                return {"available": True, "provider": "qwen3-4b-local"}
            async def generate(self, *args, **kwargs):
                return "Your notes show you chose option A [1]."

        out = asyncio.run(
            synthesis.synthesize_ask_answer(
                "which option did I pick?",
                [_item()],
                llm=_FakeLLM(),
            )
        )
        self.assertEqual(out["provider"], "qwen3-4b-local")
        self.assertIn("[1]", out["text"])
        self.assertEqual(out["confidence"], "medium")

    def test_llm_empty_text_falls_back(self) -> None:
        class _EmptyLLM:
            def status(self):
                return {"available": True, "provider": "gemini-flash"}
            async def generate(self, *args, **kwargs):
                return "   "

        out = asyncio.run(
            synthesis.synthesize_ask_answer(
                "q", [_item()], llm=_EmptyLLM(),
            )
        )
        self.assertEqual(out["provider"], "heuristic")


if __name__ == "__main__":
    unittest.main()
