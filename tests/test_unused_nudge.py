import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, update

import database as db
from database import SessionLocal, NotificationLog, Order
from models import NotificationKind, NotificationStatus
from tasks import subs

TELEGRAM_ID = 999004  # throwaway shopper, erased in tearDown
PANEL_USERNAME = f"test_{TELEGRAM_ID}"
MIN_AGE = subs.UNUSED_NUDGE_MIN_AGE


def _run(coro):
    return asyncio.run(coro)


class IsUnusedTest(unittest.TestCase):
    def test_fresh_unused_account_is_target(self):
        self.assertTrue(subs._is_unused(
            {"username": PANEL_USERNAME, "status": "active",
             "used_traffic": 0, "data_limit": 262144000, "online_at": None}
        ))

    def test_seen_account_is_skipped(self):
        self.assertFalse(subs._is_unused(
            {"status": "active", "used_traffic": 0, "data_limit": 262144000,
             "online_at": "2026-10-17T20:08:10.021321"}
        ))

    def test_used_account_is_skipped(self):
        self.assertFalse(subs._is_unused(
            {"status": "active", "used_traffic": 1, "data_limit": 262144000,
             "online_at": None}
        ))

    def test_dead_statuses_are_skipped(self):
        for status in ("limited", "expired", "disabled", "on_hold"):
            self.assertFalse(subs._is_unused(
                {"status": status, "used_traffic": 0, "data_limit": 262144000,
                 "online_at": None}
            ))

    def test_zero_or_missing_limit_is_skipped(self):
        self.assertFalse(subs._is_unused(
            {"status": "active", "used_traffic": 0, "data_limit": 0,
             "online_at": None}
        ))

    def test_panel_expired_account_is_skipped(self):
        self.assertFalse(subs._is_unused(
            {"status": "active", "used_traffic": 0, "data_limit": 100,
             "online_at": None, "expire": 1}
        ))

    def test_missing_user_is_skipped(self):
        self.assertFalse(subs._is_unused(None))
        self.assertFalse(subs._is_unused({}))


class MessageTest(unittest.TestCase):
    def test_text_names_the_subscription_and_is_html_safe(self):
        text = subs._unused_nudge_text("8014_sadragjl_2861")
        self.assertIn("8014_sadragjl_2861", text)
        self.assertIn("هنوز استفاده‌ای نشده", text)
        self.assertIn("<code>8014_sadragjl_2861</code>", text)

    def test_keyboard_has_docs_button(self):
        kb = subs._unused_nudge_keyboard()
        self.assertEqual(len(kb.inline_keyboard), 1)
        self.assertEqual(len(kb.inline_keyboard[0]), 1)
        button = kb.inline_keyboard[0][0]
        self.assertEqual(button.text, "آموزش استفاده")
        self.assertEqual(button.callback_data, "menu:docs")


class CandidateQueryTest(unittest.TestCase):
    def tearDown(self):
        _run(db.delete_test_orders(TELEGRAM_ID))

    async def _make_order(self, plan_id, status, panel_username, *,
                          age=timedelta(0), expire_in=timedelta(days=1)):
        order_id = await db.create_order(
            telegram_id=TELEGRAM_ID,
            plan_id=plan_id,
            requested_username="t",
            receipt_file_id="x",
            expire_at=datetime.now() + expire_in,
        )
        if panel_username is not None:
            await db.set_order_status(order_id, status, panel_username)
        if age:
            async with SessionLocal() as session:
                await session.execute(
                    update(Order).where(Order.order_id == order_id).values(
                        created_at=subs._utcnow_naive() - age
                    )
                )
                await session.commit()
        return order_id

    def test_candidates(self):
        now = datetime.now()
        cutoff = subs._utcnow_naive() - MIN_AGE

        async def scenario():
            old_id = await self._make_order(
                subs.TEST_PLAN_ID, "approved", PANEL_USERNAME, age=MIN_AGE * 2)
            young_id = await self._make_order(
                subs.TEST_PLAN_ID, "approved", f"test_young_{TELEGRAM_ID}")
            pending_id = await self._make_order(
                subs.TEST_PLAN_ID, "pending", None, age=MIN_AGE * 2)
            vip_id = await self._make_order(
                "plan_1m_10gb", "approved", f"vip_{TELEGRAM_ID}",
                age=MIN_AGE * 2)
            return await subs._fetch_unused_candidates(now, cutoff), (
                old_id, young_id, pending_id, vip_id)

        candidates, (old_id, young_id, pending_id, vip_id) = _run(scenario())
        ids = {o.order_id for o in candidates}

        self.assertIn(old_id, ids)
        for excluded in (young_id, pending_id, vip_id):
            self.assertNotIn(excluded, ids)


class OneTimeSendTest(unittest.TestCase):
    def tearDown(self):
        _run(db.delete_test_orders(TELEGRAM_ID))

    def test_nudge_sent_exactly_once(self):
        async def setup():
            order_id = await db.create_order(
                telegram_id=TELEGRAM_ID,
                plan_id=subs.TEST_PLAN_ID,
                requested_username="t",
                receipt_file_id="x",
                expire_at=datetime.now() + timedelta(days=1),
            )
            await db.set_order_status(order_id, "approved", PANEL_USERNAME)
            async with SessionLocal() as session:
                await session.execute(
                    update(Order).where(Order.order_id == order_id).values(
                        created_at=subs._utcnow_naive() - MIN_AGE * 2
                    )
                )
                await session.commit()
            return order_id

        order_id = _run(setup())
        panel_users = [{
            "username": PANEL_USERNAME, "status": "active",
            "used_traffic": 0, "data_limit": 262144000, "online_at": None,
        }]
        send = AsyncMock()

        with patch.object(subs.panel, "get_users_list",
                          AsyncMock(return_value=panel_users)), \
                patch.object(subs, "_send_telegram_message", send):
            subs._run_unused_nudge()
            subs._run_unused_nudge()

        self.assertEqual(send.await_count, 1)
        _, kwargs = send.await_args
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                         "menu:docs")

        async def statuses():
            async with SessionLocal() as session:
                return (await session.execute(
                    select(NotificationLog.status).where(
                        NotificationLog.order_id == order_id,
                        NotificationLog.kind == NotificationKind.UNUSED_YET,
                    )
                )).scalars().all()

        self.assertEqual(_run(statuses()), [NotificationStatus.SENT])

    def test_failed_send_is_recorded_and_not_retried(self):
        async def setup():
            order_id = await db.create_order(
                telegram_id=TELEGRAM_ID,
                plan_id=subs.TEST_PLAN_ID,
                requested_username="t",
                receipt_file_id="x",
                expire_at=datetime.now() + timedelta(days=1),
            )
            await db.set_order_status(order_id, "approved", PANEL_USERNAME)
            async with SessionLocal() as session:
                await session.execute(
                    update(Order).where(Order.order_id == order_id).values(
                        created_at=subs._utcnow_naive() - MIN_AGE * 2
                    )
                )
                await session.commit()
            return order_id

        order_id = _run(setup())
        panel_users = [{
            "username": PANEL_USERNAME, "status": "active",
            "used_traffic": 0, "data_limit": 262144000, "online_at": None,
        }]
        send = AsyncMock(side_effect=RuntimeError("bot was blocked by the user"))

        with patch.object(subs.panel, "get_users_list",
                          AsyncMock(return_value=panel_users)), \
                patch.object(subs, "_send_telegram_message", send):
            subs._run_unused_nudge()
            subs._run_unused_nudge()

        self.assertEqual(send.await_count, 1)

        async def statuses():
            async with SessionLocal() as session:
                return (await session.execute(
                    select(NotificationLog.status).where(
                        NotificationLog.order_id == order_id,
                        NotificationLog.kind == NotificationKind.UNUSED_YET,
                    )
                )).scalars().all()

        self.assertEqual(_run(statuses()), [NotificationStatus.FAILED])


if __name__ == "__main__":
    unittest.main()
