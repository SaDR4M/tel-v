"""My Subscriptions rendering for provider-backed Unlimited orders."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

import handlers.mysub as mysub
from services.subscriptions.base import SubscriptionError, SubscriptionUser


def _run(coro):
    return asyncio.run(coro)


class _FakeProvider:
    async def get(self, user_id):
        return SubscriptionUser(
            provider="dante_api",
            user_id=user_id,
            username="unlimited_77",
            subscription_url="https://sub.example.com/u",
            status="active",
            expires_at=datetime.now() + timedelta(days=20),
            quota_bytes=0,
        )


class _BrokenProvider:
    async def get(self, user_id):
        raise SubscriptionError("down")


class UnlimitedMysubTest(unittest.TestCase):
    ORDER = {
        "order_id": 1,
        "provider": "dante_api",
        "provider_user_id": "dante-77",
        "provider_username": "unlimited_77",
        "subscription_url": "https://stored.example.com/u",
        "plan_id": "plan_u_300gb",
        "panel_username": None,
    }

    def test_renders_live_unlimited_subscription(self):
        with patch("database.get_order", AsyncMock(return_value=self.ORDER)), \
             patch(
                 "services.subscriptions.registry.get_provider",
                 return_value=_FakeProvider(),
             ):
            text = _run(mysub._build_sub_text(1))

        self.assertIn("https://sub.example.com/u", text)
        self.assertIn("نامحدود", text)
        self.assertIn("فعال", text)

    def test_falls_back_to_stored_link_when_provider_is_down(self):
        with patch("database.get_order", AsyncMock(return_value=self.ORDER)), \
             patch(
                 "services.subscriptions.registry.get_provider",
                 return_value=_BrokenProvider(),
             ):
            text = _run(mysub._build_sub_text(1))

        self.assertIn("https://stored.example.com/u", text)


if __name__ == "__main__":
    unittest.main()
