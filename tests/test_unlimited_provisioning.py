"""Provider-agnostic approval orchestration for the Unlimited plan."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

import database as db
from config import settings
from services.subscriptions.base import ProvisionRequest, SubscriptionUser
from services.subscriptions.provisioning import approve_purchase, approve_renewal


def _run(coro):
    return asyncio.run(coro)


class _FakeProvider:
    name = "dante_api"

    def __init__(self, expire_at):
        self.expire_at = expire_at
        self.created = None
        self.extended = None

    async def create(self, request: ProvisionRequest):
        self.created = request
        return SubscriptionUser(
            provider=self.name,
            user_id="dante-11",
            username="u42bdb27e@vpn",
            subscription_url="https://sub.example.com/new",
            status="active",
            expires_at=self.expire_at,
            quota_bytes=0,
        )

    async def extend(self, user_id: str, request: ProvisionRequest):
        self.extended = (user_id, request)
        return SubscriptionUser(
            provider=self.name,
            user_id=user_id,
            username="u42bdb27e@vpn",
            subscription_url="https://sub.example.com/same",
            status="active",
            expires_at=self.expire_at,
            quota_bytes=0,
        )


class UnlimitedProvisioningTest(unittest.TestCase):
    TG = 999310

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(self.TG))

    def _new_order(self):
        return _run(db.create_order(
            telegram_id=self.TG,
            plan_id="plan_u_300gb",
            requested_username="ali",
            receipt_file_id="test",
            expire_at=datetime.now() + timedelta(days=1),
        ))

    def _new_renewal(self, provider_user_id="dante-11"):
        return _run(db.create_order(
            telegram_id=self.TG,
            plan_id="plan_u_300gb",
            requested_username="ali",
            receipt_file_id="test",
            expire_at=datetime.now() + timedelta(days=1),
            order_type="renewal",
            target_provider="dante_api",
            target_provider_user_id=provider_user_id,
        ))

    def test_approval_creates_and_persists_provider_account(self):
        order_id = self._new_order()
        order = _run(db.get_order(order_id))
        plan = settings.get_plan("plan_u_300gb")
        expire_at = datetime.now() + timedelta(days=30)
        fake = _FakeProvider(expire_at)

        with patch(
            "services.subscriptions.provisioning.get_provider",
            return_value=fake,
        ):
            result = _run(approve_purchase(order, plan))

        saved = _run(db.get_order(order_id))
        self.assertFalse(result.extended)
        self.assertEqual(saved["provider"], "dante_api")
        self.assertEqual(saved["provider_user_id"], "dante-11")
        self.assertEqual(saved["provider_username"], "u42bdb27e@vpn")
        self.assertEqual(saved["subscription_url"], "https://sub.example.com/new")
        self.assertEqual(saved["status"], "active")
        self.assertEqual(saved["expire_at"], expire_at)
        self.assertEqual(fake.created.order_id, order_id)

    def test_new_purchase_creates_new_user_even_for_same_buyer(self):
        old_id = self._new_order()
        _run(db.approve_provider_order(
            old_id,
            provider="dante_api",
            provider_user_id="dante-old",
            provider_username="old@vpn",
            subscription_url="https://sub.example.com/old",
            expire_at=datetime.now() + timedelta(days=1),
        ))

        new_id = self._new_order()
        order = _run(db.get_order(new_id))
        plan = settings.get_plan("plan_u_300gb")
        expire_at = datetime.now() + timedelta(days=30)
        fake = _FakeProvider(expire_at)

        with patch(
            "services.subscriptions.provisioning.get_provider",
            return_value=fake,
        ):
            result = _run(approve_purchase(order, plan))

        old = _run(db.get_order(old_id))
        saved = _run(db.get_order(new_id))
        self.assertFalse(result.extended)
        self.assertIsNone(old["superseded_at"])
        self.assertIsNone(saved["superseded_at"])
        self.assertEqual(saved["provider_user_id"], "dante-11")
        self.assertEqual(saved["subscription_url"], "https://sub.example.com/new")
        self.assertEqual(fake.extended, None)
        self.assertEqual(fake.created.order_id, new_id)

    def test_explicit_renewal_extends_and_supersedes_old_order(self):
        old_id = self._new_order()
        _run(db.approve_provider_order(
            old_id,
            provider="dante_api",
            provider_user_id="dante-11",
            provider_username="existing@vpn",
            subscription_url="https://sub.example.com/same",
            expire_at=datetime.now() + timedelta(days=1),
        ))

        new_id = self._new_renewal()
        order = _run(db.get_order(new_id))
        plan = settings.get_plan("plan_u_300gb")
        expire_at = datetime.now() + timedelta(days=30)
        fake = _FakeProvider(expire_at)

        with patch(
            "services.subscriptions.provisioning.get_provider",
            return_value=fake,
        ):
            result = _run(approve_renewal(order, plan))

        old = _run(db.get_order(old_id))
        saved = _run(db.get_order(new_id))
        self.assertTrue(result.extended)
        self.assertIsNotNone(old["superseded_at"])
        self.assertIsNone(saved["superseded_at"])
        self.assertEqual(saved["provider_user_id"], "dante-11")
        self.assertEqual(saved["subscription_url"], "https://sub.example.com/same")
        self.assertEqual(saved["expire_at"], expire_at)
        self.assertEqual(fake.extended[0], "dante-11")


if __name__ == "__main__":
    unittest.main()
