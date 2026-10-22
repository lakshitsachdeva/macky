from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.indexer import embedder


class EmbedderBundleTests(unittest.TestCase):
    @staticmethod
    def _make_minimal_bundled_model(model_dir: Path) -> None:
        model_dir.mkdir(parents=True)
        for filename in embedder.BUNDLED_MODEL_REQUIRED_FILES:
            (model_dir / filename).write_text("{}", encoding="utf-8")

    def test_resolve_embedding_model_target_uses_bundle_for_packaged_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = (
                Path(tmpdir) / "models" / "nomic-embed-text-v1.5"
            )
            self._make_minimal_bundled_model(model_dir)

            with mock.patch.object(embedder.sys, "frozen", True, create=True), mock.patch.object(
                embedder.sys,
                "_MEIPASS",
                tmpdir,
                create=True,
            ):
                target = embedder.resolve_embedding_model_target("custom-model")

            self.assertEqual(target, str(model_dir))

    def test_resolve_embedding_model_target_uses_hub_name_in_development(self) -> None:
        with mock.patch.object(embedder.sys, "frozen", False, create=True):
            if hasattr(embedder.sys, "_MEIPASS"):
                with mock.patch.object(embedder.sys, "_MEIPASS", None, create=True):
                    target = embedder.resolve_embedding_model_target("custom-model")
            else:
                target = embedder.resolve_embedding_model_target("custom-model")

        self.assertEqual(target, "custom-model")

    def test_resolve_embedding_model_target_raises_when_bundle_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            embedder.sys,
            "frozen",
            True,
            create=True,
        ), mock.patch.object(
            embedder.sys,
            "_MEIPASS",
            tmpdir,
            create=True,
        ):
            with self.assertRaises(FileNotFoundError):
                embedder.resolve_embedding_model_target("custom-model")

    def test_resolve_embedding_model_target_raises_when_bundle_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = Path(tmpdir) / "models" / "nomic-embed-text-v1.5"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "modules.json").write_text("{}", encoding="utf-8")

            with mock.patch.object(embedder.sys, "frozen", True, create=True), mock.patch.object(
                embedder.sys,
                "_MEIPASS",
                tmpdir,
                create=True,
            ):
                with self.assertRaises(FileNotFoundError):
                    embedder.resolve_embedding_model_target("custom-model")


if __name__ == "__main__":
    unittest.main()
