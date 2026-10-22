from __future__ import annotations

import unittest
from unittest import mock

from backend import main


class SearchDebugResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_items_returns_debug_payload_when_explain_enabled(self) -> None:
        body = main.SearchRequest(query="swarm alpha", limit=5, explain=True)

        with mock.patch.object(
            main.search,
            "search",
            new=mock.AsyncMock(return_value=([], {"pipeline": "fast", "complexity_score": 0.2})),
        ) as search_mock, mock.patch.object(
            main,
            "_serialize_search_results",
            return_value=[],
        ):
            response = await main.search_items(body=body, explain=False, user={})

        self.assertIsInstance(response, dict)
        self.assertIn("results", response)
        self.assertIn("debug", response)
        self.assertEqual(response["debug"]["pipeline"], "fast")
        self.assertTrue(search_mock.await_args.kwargs.get("return_debug"))

    async def test_search_items_returns_plain_list_without_explain(self) -> None:
        body = main.SearchRequest(query="resume", limit=5, explain=False)

        with mock.patch.object(
            main.search,
            "search",
            new=mock.AsyncMock(return_value=[]),
        ) as search_mock, mock.patch.object(
            main,
            "_serialize_search_results",
            return_value=[{"id": "row-1"}],
        ):
            response = await main.search_items(body=body, explain=False, user={})

        self.assertIsInstance(response, list)
        self.assertEqual(len(response), 1)
        self.assertFalse(search_mock.await_args.kwargs.get("return_debug", False))


if __name__ == "__main__":
    unittest.main()
