from __future__ import annotations

import asyncio
import base64
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from backend import auth


def _token_for_user(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "email": "cached@example.com",
        "exp": 1,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"header.{encoded.rstrip('=')}.signature"


class AuthCacheTests(unittest.TestCase):
    def test_expired_supabase_token_can_use_fresh_desktop_cache(self) -> None:
        token = _token_for_user("cached-user-1")
        now = datetime.now(timezone.utc).isoformat()

        cached_payload = {
            "users": {
                "cached-user-1": {
                    "id": "cached-user-1",
                    "email": "cached@example.com",
                    "name": "Cached User",
                    "verified_at": now,
                },
            },
            "access": {
                "cached-user-1": {
                    "has_access": True,
                    "tier": "whitelist",
                    "days_remaining": 999,
                    "expires_at": None,
                    "is_blocked": False,
                    "verified_at": now,
                },
            },
        }

        with mock.patch.object(
            auth,
            "_fetch_supabase_user",
            side_effect=auth.SupabaseHttpError(401, "Token is expired"),
        ), mock.patch.object(
            auth,
            "_read_auth_cache",
            return_value=cached_payload,
        ), mock.patch.object(
            auth,
            "_write_auth_cache",
        ):
            user = asyncio.run(auth.get_current_user(f"Bearer {token}"))

        self.assertEqual(user["id"], "cached-user-1")
        self.assertEqual(user["email"], "cached@example.com")
        self.assertTrue(user["offline_cached"])


if __name__ == "__main__":
    unittest.main()
