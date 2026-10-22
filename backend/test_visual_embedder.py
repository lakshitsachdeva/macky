import unittest
from pathlib import Path

from backend.indexer.visual_embedder import _compose_visual_search_text


class VisualEmbedderTextTests(unittest.TestCase):
    def test_empty_ocr_falls_back_to_filename_caption(self):
        text, source = _compose_visual_search_text(
            Path("/Users/test/Downloads/example.jpeg"),
            "",
            text_source="",
        )
        self.assertEqual(text, "Image file example in Downloads")
        self.assertEqual(source, "filename")

    def test_partial_ocr_is_preserved_for_search(self):
        text, source = _compose_visual_search_text(
            Path("/Users/test/Downloads/poster.jpeg"),
            "IIT Madras fellowship",
            text_source="ocr_success",
        )
        self.assertIn("Image file poster in Downloads", text)
        self.assertIn("Visible text: IIT Madras fellowship", text)
        self.assertEqual(source, "ocr_partial")

    def test_useful_ocr_stays_primary_text(self):
        text, source = _compose_visual_search_text(
            Path("/Users/test/Downloads/poster.jpeg"),
            "Indian Institute of Technology Madras Summer Fellowship Programme 2026",
            text_source="ocr_success",
        )
        self.assertEqual(
            text,
            "Indian Institute of Technology Madras Summer Fellowship Programme 2026",
        )
        self.assertEqual(source, "ocr_success")


if __name__ == "__main__":
    unittest.main()
