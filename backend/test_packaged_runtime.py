from __future__ import annotations

import os
import unittest
from unittest import mock

from backend import main


class PackagedRuntimeTests(unittest.TestCase):
    def test_packaged_runtime_sets_offline_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            main.sys,
            "frozen",
            True,
            create=True,
        ), mock.patch.object(
            main.sys,
            "_MEIPASS",
            "/tmp/recurdo-bundle",
            create=True,
        ):
            main._configure_packaged_offline_runtime()

            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
            self.assertEqual(os.environ["HF_DATASETS_OFFLINE"], "1")
            self.assertEqual(os.environ["HF_HUB_DISABLE_XET"], "1")

    def test_development_runtime_leaves_env_unchanged(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            main.sys,
            "frozen",
            False,
            create=True,
        ):
            if hasattr(main.sys, "_MEIPASS"):
                with mock.patch.object(main.sys, "_MEIPASS", None, create=True):
                    main._configure_packaged_offline_runtime()
            else:
                main._configure_packaged_offline_runtime()

            self.assertNotIn("HF_HUB_OFFLINE", os.environ)


if __name__ == "__main__":
    unittest.main()
