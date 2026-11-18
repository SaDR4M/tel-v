"""DANTE Unlimited provider adapter tests (no network calls)."""
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from config import settings
from services.subscriptions.base import ProvisionRequest
from services.subscriptions.dante_api import (
    DanteClient,
    DanteProvider,
    _expires_date,
    _to_subscription,
)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self._payload

    async def text(self):
        return ""


class _FakeSession:
    calls = []

    def __init__(self, *args, **kwargs):
        self.calls = _FakeSession.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeResponse(
            {
                "data": {
                    "id": 7,
                    "name": "Ali",
                    "email": "u42bdb27e@vpn",
                    "status": "active",
                    "enabled": True,
                    "quota_bytes": 0,
                    "used_bytes": 0,
                    "expires_at": int(
                        (datetime.now() + timedelta(days=30)).timestamp()
                    ),
                    "sub_url": "https://sub.example.com/ali",
                }
            }
        )


class DanteClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_sends_unlimited_payload_without_configs(self):
        _FakeSession.calls = []
        with patch.object(settings, "DANTE_API_KEY", "test-key"), \
             patch(
                 "services.subscriptions.dante_api.aiohttp.ClientSession",
                 _FakeSession,
             ):
            data = await DanteClient().create_user(
                name="ali_42_o9",
                expires_date="2026-12-18",
                note="bot order #9",
            )

        method, url, kwargs = _FakeSession.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/users"))
        self.assertEqual(kwargs["json"]["quota_gb"], 0)
        self.assertNotIn("configs", kwargs["json"])
        self.assertEqual(kwargs["json"]["expires_date"], "2026-12-18")
        self.assertEqual(data["id"], 7)

    def test_expires_date_is_iso(self):
        self.assertRegex(_expires_date(30), r"^\d{4}-\d{2}-\d{2}$")


class _FakeProviderClient:
    def __init__(self):
        self.created = None
        self.extended = None

    async def create_user(self, **kwargs):
        self.created = kwargs
        return {
            "id": 11,
            "name": kwargs["name"],
            "email": "u42bdb27e@vpn",
            "status": "active",
            "enabled": True,
            "expires_at": int((datetime.now() + timedelta(days=30)).timestamp()),
            "sub_url": "https://sub.example.com/new",
            "quota_bytes": 0,
        }

    async def extend_user(self, user_id, expires_date):
        self.extended = (user_id, expires_date)
        return {
            "id": 11,
            "name": "Ali",
            "email": "u42bdb27e@vpn",
            "status": "active",
            "enabled": True,
            "expires_at": int((datetime.now() + timedelta(days=30)).timestamp()),
            "sub_url": "https://sub.example.com/same",
            "quota_bytes": 0,
        }


class DanteProviderTest(unittest.IsolatedAsyncioTestCase):
    def _request(self):
        return ProvisionRequest(
            order_id=9,
            telegram_id=42,
            requested_username="Ali",
            plan={"duration_days": 30},
        )

    async def test_create_uses_requested_name_and_api_unique_id(self):
        provider = DanteProvider()
        fake = _FakeProviderClient()
        provider.client = fake
        user = await provider.create(self._request())

        self.assertEqual(fake.created["name"], "Ali")
        self.assertRegex(fake.created["expires_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(user.provider, "dante_api")
        self.assertEqual(user.user_id, "11")
        self.assertEqual(user.username, "u42bdb27e@vpn")
        self.assertEqual(user.status, "active")
        self.assertEqual(user.subscription_url, "https://sub.example.com/new")

    async def test_extend_patches_existing_provider_user(self):
        provider = DanteProvider()
        fake = _FakeProviderClient()
        provider.client = fake
        user = await provider.extend("11", self._request())

        self.assertEqual(fake.extended[0], "11")
        self.assertRegex(fake.extended[1], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(user.user_id, "11")
        self.assertEqual(user.subscription_url, "https://sub.example.com/same")

    def test_status_mapping(self):
        self.assertEqual(_to_subscription({"id": 1, "status": "limited"}).status,
                         "limited")
        self.assertEqual(
            _to_subscription({"id": 1, "status": "active", "enabled": False}).status,
            "disabled",
        )
        old = int(datetime.now().timestamp()) - 3600
        self.assertEqual(
            _to_subscription({"id": 1, "status": "weird", "expires_at": old}).status,
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
