from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from backend import auth


class LocalModeAuthTests(unittest.TestCase):
    def test_local_mode_grants_access_without_remote_verification(self) -> None:
        with mock.patch.object(auth, "LOCAL_MODE", True):
            access = auth.get_access_info("local-dev-user")

        self.assertTrue(access["has_access"])
        self.assertEqual(access["tier"], "local-forensics")
        self.assertFalse(access["verification_unavailable"])

    def test_local_mode_accepts_requests_without_bearer_header(self) -> None:
        async def run() -> dict:
            with mock.patch.object(auth, "LOCAL_MODE", True):
                return await auth.get_current_user(None)

        user = asyncio.run(run())

        self.assertEqual(user["id"], "local-dev-user")
        self.assertEqual(user["email"], "local@custody.app")
        self.assertEqual(user["token"], "local-dev-token")


if __name__ == "__main__":
    unittest.main()
