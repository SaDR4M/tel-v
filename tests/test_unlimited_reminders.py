"""Expiry reminders for provider-backed Unlimited orders."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

from sqlalchemy import select

import database as db
import tasks.subs as subs
from database import NotificationLog, Order, SessionLocal
from services.subscriptions.base import SubscriptionUser


def _run(coro):
    return asyncio.run(coro)


async def _orm_order(order_id):
    async with SessionLocal() as session:
        return await session.get(Order, order_id)


async def _logs(order_id):
    async with SessionLocal() as session:
        result = await session.execute(
            select(NotificationLog).where(
                NotificationLog.order_id == order_id
            )
        )
        return result.scalars().all()


class UnlimitedExpiryReminderTest(unittest.TestCase):
    TG = 999320

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(self.TG))

    def _order(self):
        order_id = _run(db.create_order(
            telegram_id=self.TG,
            plan_id="plan_u_300gb",
            requested_username="unlimited",
            receipt_file_id="test",
            expire_at=datetime.now() + timedelta(days=1),
        ))
        _run(db.approve_provider_order(
            order_id,
            provider="dante_api",
            provider_user_id="dante-77",
            provider_username="unlimited_77",
            subscription_url="https://sub.example.com/u",
            expire_at=datetime.now() + timedelta(days=1),
        ))
        return order_id

    def test_active_unlimited_order_is_scanned(self):
        order_id = self._order()
        with patch.object(
            subs, "_active_dante_ids", AsyncMock(return_value={"dante-77"})
        ):
            orders = _run(subs._scan_subscriptions_expire_date())
        self.assertIn(order_id, [order.order_id for order in orders])

    def test_inactive_unlimited_order_is_not_scanned(self):
        self._order()
        with patch.object(
            subs, "_active_dante_ids", AsyncMock(return_value=set())
        ):
            orders = _run(subs._scan_subscriptions_expire_date())
        self.assertEqual(orders, [])

    def test_status_sync_updates_provider_order(self):
        order_id = self._order()
        orm = _run(_orm_order(order_id))
        fake = SimpleNamespace(
            list_users=AsyncMock(return_value=[
                SubscriptionUser(
                    provider="dante_api",
                    user_id="dante-77",
                    username="unlimited_77",
                    subscription_url="https://sub.example.com/u",
                    status="expired",
                )
            ])
        )
        with patch.object(
            subs, "_fetch_provisioned_orders",
            AsyncMock(return_value=[orm]),
        ), patch.object(subs, "get_provider", return_value=fake):
            subs.sync_order_statuses()

        self.assertEqual(_run(db.get_order(order_id))["status"], "expired")

    def test_expiry_reminder_sends_once(self):
        order_id = self._order()
        with patch.object(
            subs, "_active_dante_ids", AsyncMock(return_value={"dante-77"})
        ), patch.object(
            subs, "_send_telegram_message", AsyncMock()
        ) as send:
            subs.send_expire_reminder()
            subs.send_expire_reminder()

        send.assert_awaited_once()
        logs = _run(_logs(order_id))
        self.assertEqual(
            [log.status for log in logs if log.kind == subs.NotificationKind.EXPIRY_2D],
            [subs.NotificationStatus.SENT],
        )


if __name__ == "__main__":
    unittest.main()
