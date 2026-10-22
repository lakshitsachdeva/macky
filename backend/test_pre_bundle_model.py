from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import pre_bundle_model


class PreBundleModelTests(unittest.TestCase):
    def test_rewrite_auto_map_to_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = Path(tmpdir)
            config_path = model_dir / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "auto_map": {
                            "AutoConfig": (
                                "nomic-ai/nomic-bert-2048--"
                                "configuration_hf_nomic_bert.NomicBertConfig"
                            ),
                            "AutoModel": (
                                "nomic-ai/nomic-bert-2048--"
                                "modeling_hf_nomic_bert.NomicBertModel"
                            ),
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(pre_bundle_model, "LOCAL_MODEL_DIR", model_dir):
                pre_bundle_model._rewrite_auto_map_to_local_paths()

            rewritten = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                rewritten["auto_map"]["AutoConfig"],
                "configuration_hf_nomic_bert.NomicBertConfig",
            )
            self.assertEqual(
                rewritten["auto_map"]["AutoModel"],
                "modeling_hf_nomic_bert.NomicBertModel",
            )

    def test_rewrite_auto_map_noop_without_auto_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = Path(tmpdir)
            config_path = model_dir / "config.json"
            config_path.write_text(json.dumps({"model_type": "nomic_bert"}), encoding="utf-8")

            with mock.patch.object(pre_bundle_model, "LOCAL_MODEL_DIR", model_dir):
                pre_bundle_model._rewrite_auto_map_to_local_paths()

            rewritten = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(rewritten["model_type"], "nomic_bert")


if __name__ == "__main__":
    unittest.main()
