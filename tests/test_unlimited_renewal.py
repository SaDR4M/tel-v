"""Unlimited subscriptions in the renewal menu."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

import handlers.renewal as renewal
from services.subscriptions.base import SubscriptionUser


def _run(coro):
    return asyncio.run(coro)


def _provider_user(expires_in_days=1):
    return SubscriptionUser(
        provider="dante_api",
        user_id="dante-11",
        username="u42bdb27e@vpn",
        subscription_url="https://sub.example.com/u",
        status="active",
        expires_at=datetime.now() + timedelta(days=expires_in_days),
        quota_bytes=0,
    )


class UnlimitedRenewalMenuTest(unittest.TestCase):
    def _fetch(self, user, order_expire=None):
        provider = AsyncMock()
        provider.name = "dante_api"
        provider.get.return_value = user
        order = {
            "order_id": 7,
            "provider_user_id": "dante-11",
            "requested_username": "sadra",
            "expire_at": order_expire,
        }
        with patch.object(
            renewal.db, "get_distinct_panel_usernames",
            AsyncMock(return_value=[]),
        ), patch.object(
            renewal.db, "get_display_names", AsyncMock(return_value={})
        ), patch.object(
            renewal.db, "get_provider_orders",
            AsyncMock(return_value=[order]),
        ), patch.object(
            renewal, "get_provider", return_value=provider
        ):
            return _run(renewal._fetch_renewable_subs(42)), provider

    def test_unlimited_near_expiry_is_renewable(self):
        subs, _ = self._fetch(_provider_user(1))
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["provider"], "dante_api")
        self.assertEqual(subs[0]["target"], "dante-11")
        self.assertIn("sadra", subs[0]["label"])
        self.assertIn("نامحدود", subs[0]["label"])

    def test_unlimited_far_from_expiry_is_not_renewable(self):
        subs, _ = self._fetch(_provider_user(10))
        self.assertEqual(subs, [])

    def test_db_expiry_is_used_when_api_has_none(self):
        user = _provider_user(10)
        user = SubscriptionUser(**{**user.__dict__, "expires_at": None})
        subs, _ = self._fetch(
            user, order_expire=datetime.now() + timedelta(days=1)
        )
        self.assertEqual(len(subs), 1)


if __name__ == "__main__":
    unittest.main()
