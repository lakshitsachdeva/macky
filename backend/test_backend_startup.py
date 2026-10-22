from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from backend import main


class _FakeWatcher:
    def __init__(self) -> None:
        self.started = False
        self.stop_calls = 0

    def start(self) -> None:
        time.sleep(0.25)
        self.started = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.started = False


async def _idle_background_task() -> None:
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise


class BackendStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_does_not_block_on_watcher_start(self) -> None:
        fake_watcher = _FakeWatcher()
        lifespan_cm = main.lifespan(main.app)

        with mock.patch.object(main, "broadcast_events", side_effect=_idle_background_task), mock.patch.object(
            main,
            "_run_pending_retry_loop",
            side_effect=_idle_background_task,
        ), mock.patch.object(
            main,
            "_run_keepalive_loop",
            side_effect=_idle_background_task,
        ), mock.patch.object(
            main,
            "startup_health_check",
            new=mock.AsyncMock(return_value={"status": "ok", "actions": [], "issues": []}),
        ), mock.patch.object(
            main.watcher,
            "load_watched_dirs",
            return_value=[],
        ), mock.patch.object(
            main.watcher,
            "MemoryEngineWatcher",
            return_value=fake_watcher,
        ), mock.patch.object(
            main.pipeline,
            "bootstrap_deferred_visual_ocr",
            new=mock.AsyncMock(return_value=0),
        ), mock.patch.object(
            main,
            "_get_item_count",
            return_value=0,
        ), mock.patch.object(
            main,
            "_warm_backend_assets",
            new=mock.AsyncMock(return_value=None),
        ), mock.patch.object(
            main.embedder,
            "save_text_index_if_loaded",
        ):
            started_at = time.perf_counter()
            await asyncio.wait_for(lifespan_cm.__aenter__(), timeout=0.15)
            elapsed = time.perf_counter() - started_at

            self.assertLess(elapsed, 0.15)

            await lifespan_cm.__aexit__(None, None, None)

        self.assertEqual(fake_watcher.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
