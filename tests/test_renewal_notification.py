"""End-to-end tests for the renewal notification fix: once a subscription is
renewed, the order row it replaced must never produce another reminder; only
the order created by the renewal may, and only for its own cycle."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select

import database as db
import handlers.admin_review as admin_review
from config import settings
from database import NotificationLog, Order, SessionLocal
from models import NotificationKind, NotificationStatus
from tasks import subs

SHOPPER = 999006  # throwaway buyer, erased in tearDown
PLAN_ID = "plan_1m_10gb"


def _run(coro):
    return asyncio.run(coro)


def _admin_id():
    return next(iter(settings.ADMIN_USER_IDS))


def _username(tag: str) -> str:
    return f"notice_{tag}_{SHOPPER}"


def _order(
    username: str,
    expires_in: timedelta,
    *,
    approve: bool = True,
    order_type: str = "purchase",
) -> int:
    order_id = _run(db.create_order(
        telegram_id=SHOPPER,
        plan_id=PLAN_ID,
        requested_username="notice",
        receipt_file_id="t",
        expire_at=datetime.now() + expires_in,
        order_type=order_type,
        target_panel_username=username if order_type == "renewal" else None,
    ))
    if approve:
        _run(db.set_order_status(order_id, "approved", username))
    return order_id


def _logs(*order_ids):
    async def fetch():
        async with SessionLocal() as session:
            result = await session.execute(
                select(NotificationLog).where(
                    NotificationLog.order_id.in_(order_ids)
                )
            )
            return result.scalars().all()

    return _run(fetch())


def _sends_to_shopper(send_mock):
    return [
        call for call in send_mock.await_args_list
        if call.args and call.args[0] == SHOPPER
    ]


async def _all_panel_usernames():
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order.panel_username).where(Order.panel_username.isnot(None))
        )
        return {row[0] for row in result}


def _run_expiry_pass():
    """Execute the real Celery task body (no broker/worker needed)."""
    active = _run(_all_panel_usernames())
    with patch.object(
        subs, "_active_panel_usernames", AsyncMock(return_value=active)
    ):
        subs.send_expire_reminder()


def _approve_renewal(order_id: int, panel_expire_ts: int | None):
    return_value = {"subscription_url": "https://sub/unchanged"}
    if panel_expire_ts is not None:
        return_value["expire"] = panel_expire_ts
    bot = SimpleNamespace(send_message=AsyncMock())
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=_admin_id(), first_name="Admin"),
        data=f"admin_approve:{order_id}",
        message=SimpleNamespace(caption="رسید", edit_caption=AsyncMock()),
        answer=AsyncMock(),
    )
    with patch.object(
        admin_review.panel, "renew_user", AsyncMock(return_value=return_value)
    ), patch("database.link_panel_account", AsyncMock()):
        _run(admin_review.cb_admin_approve(callback, bot))
    return bot


class RenewalNotificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(SHOPPER))

    def test_renewal_silences_the_old_order_end_to_end(self):
        username = _username("e2e")
        old_id = _order(username, timedelta(days=1))  # inside the 2-day window
        renewal_id = _order(
            username, timedelta(days=30), approve=False, order_type="renewal"
        )
        panel_expire = int((datetime.now() + timedelta(days=30)).timestamp())

        # Negative control: at approval time the old row really is eligible.
        with patch.object(
            subs, "_active_panel_usernames", AsyncMock(return_value={username})
        ):
            self.assertIn(
                old_id,
                {o.order_id for o in _run(subs._scan_subscriptions_expire_date())},
            )

        with patch.object(subs, "_send_telegram_message", AsyncMock()) as send:
            bot = _approve_renewal(renewal_id, panel_expire)
            self.assertIn("تمدید", bot.send_message.await_args.kwargs["text"])

            # The old order's stale expiry is still inside the window, but the
            # renewal must have silenced it.
            _run_expiry_pass()
            self.assertEqual(_sends_to_shopper(send), [])
            self.assertEqual(_logs(old_id), [])
            self.assertEqual(_logs(renewal_id), [])

            # Next cycle: the renewed order enters the window and reminds once.
            _run(db.set_order_expire_at(renewal_id, datetime.now() + timedelta(days=1)))
            _run_expiry_pass()

        sends = _sends_to_shopper(send)
        self.assertEqual(len(sends), 1)
        self.assertIn(settings.get_plan(PLAN_ID)["name"], sends[0].args[1])
        new_logs = _logs(renewal_id)
        self.assertEqual(len(new_logs), 1)
        self.assertEqual(new_logs[0].kind, NotificationKind.EXPIRY_2D)
        self.assertEqual(new_logs[0].status, NotificationStatus.SENT)
        self.assertEqual(_logs(old_id), [])

    def test_cycle_already_reminded_before_renewal_is_not_resent(self):
        username = _username("pre-sent")
        old_id = _order(username, timedelta(days=1))
        old_expire = _run(db.get_order(old_id))["expire_at"]

        # Simulate the reminder that went out before the user renewed.
        self.assertTrue(_run(subs._claim_notification(
            old_id, NotificationKind.EXPIRY_2D, old_expire
        )))
        _run(subs._mark_notification(
            old_id, NotificationKind.EXPIRY_2D, old_expire,
            NotificationStatus.SENT,
        ))

        renewal_id = _order(username, timedelta(days=30))
        _run(db.supersede_orders_for_panel(username, keep_order_id=renewal_id))

        with patch.object(subs, "_send_telegram_message", AsyncMock()) as send:
            _run_expiry_pass()

        self.assertEqual(_sends_to_shopper(send), [])
        self.assertEqual(len(_logs(old_id)), 1)  # history untouched, no duplicate
        self.assertEqual(_logs(renewal_id), [])

    def test_current_order_is_reminded_exactly_once(self):
        username = _username("once")
        order_id = _order(username, timedelta(days=1))

        with patch.object(subs, "_send_telegram_message", AsyncMock()) as send:
            _run_expiry_pass()
            _run_expiry_pass()  # overlapping beat run

        self.assertEqual(len(_sends_to_shopper(send)), 1)
        logs = _logs(order_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].status, NotificationStatus.SENT)

    def test_chain_of_renewals_only_the_latest_reminds(self):
        username = _username("chain")
        first = _order(username, timedelta(days=1))
        second = _order(username, timedelta(days=1))
        latest = _order(username, timedelta(days=1))

        _run(db.supersede_orders_for_panel(username, keep_order_id=latest))

        with patch.object(subs, "_send_telegram_message", AsyncMock()) as send:
            _run_expiry_pass()

        self.assertEqual(len(_sends_to_shopper(send)), 1)
        self.assertEqual(_logs(first), [])
        self.assertEqual(_logs(second), [])
        self.assertEqual(len(_logs(latest)), 1)

    def test_plain_purchase_is_still_reminded(self):
        order_id = _order(_username("plain"), timedelta(days=1))

        with patch.object(subs, "_send_telegram_message", AsyncMock()) as send:
            _run_expiry_pass()

        self.assertEqual(len(_sends_to_shopper(send)), 1)
        self.assertEqual(len(_logs(order_id)), 1)

    def test_usage_reminder_belongs_to_the_current_order_only(self):
        username = _username("usage")
        old_id = _order(username, timedelta(days=1))
        current_id = _order(username, timedelta(days=30))
        _run(db.supersede_orders_for_panel(username, keep_order_id=current_id))

        panel_user = {
            "username": username,
            "status": "active",
            "data_limit": 100,
            "used_traffic": 95,
        }
        with patch.object(
            subs, "_scan_subscriptions_limit_usage",
            AsyncMock(return_value=[panel_user]),
        ), patch.object(subs, "_send_telegram_message", AsyncMock()) as send:
            subs.send_limit_reminder()

        self.assertEqual(len(_sends_to_shopper(send)), 1)
        usage_new = [
            log for log in _logs(current_id)
            if log.kind == NotificationKind.USAGE_90
        ]
        self.assertEqual(len(usage_new), 1)
        self.assertEqual(usage_new[0].status, NotificationStatus.SENT)
        self.assertEqual(_logs(old_id), [])

    def test_approval_without_panel_expire_falls_back_to_plan_duration(self):
        username = _username("fallback")
        old_id = _order(username, timedelta(days=1))
        renewal_id = _order(
            username, timedelta(days=30), approve=False, order_type="renewal"
        )

        _approve_renewal(renewal_id, panel_expire_ts=None)

        renewed = _run(db.get_order(renewal_id))
        remaining = renewed["expire_at"] - datetime.now()
        expected = timedelta(days=settings.get_plan(PLAN_ID)["duration_days"])
        self.assertGreater(remaining, expected - timedelta(minutes=5))
        self.assertLess(remaining, expected + timedelta(minutes=5))
        self.assertIsNone(renewed["superseded_at"])
        self.assertIsNotNone(_run(db.get_order(old_id))["superseded_at"])


class UsageReminderStatusTest(unittest.TestCase):
    """Usage warnings must include limited users and the exact 90% boundary."""

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(SHOPPER))

    def _run_limit_pass(self, panel_users):
        with patch.object(
            subs.panel, "get_users_list",
            AsyncMock(return_value=panel_users),
        ) as fetch, patch.object(
            subs, "_send_telegram_message", AsyncMock(),
        ) as send:
            subs.send_limit_reminder()
        return fetch, send

    def test_limited_user_is_not_reminded(self):
        username = _username("limited")
        order_id = _order(username, timedelta(days=30))
        fetch, send = self._run_limit_pass([{
            "username": username, "status": "limited",
            "used_traffic": 95, "data_limit": 100,
        }])

        fetch.assert_awaited_once_with(status="active")
        self.assertEqual(_sends_to_shopper(send), [])
        self.assertEqual(
            [log for log in _logs(order_id)
             if log.kind == NotificationKind.USAGE_90],
            [],
        )

    def test_exactly_ninety_percent_is_reminded(self):
        username = _username("exact90")
        _order(username, timedelta(days=30))
        _, send = self._run_limit_pass([{
            "username": username, "status": "active",
            "used_traffic": 90, "data_limit": 100,
        }])
        self.assertEqual(len(_sends_to_shopper(send)), 1)

    def test_below_ninety_percent_is_not_reminded(self):
        username = _username("below90")
        _order(username, timedelta(days=30))
        _, send = self._run_limit_pass([{
            "username": username, "status": "active",
            "used_traffic": 89.9, "data_limit": 100,
        }])
        self.assertEqual(_sends_to_shopper(send), [])

    def test_expired_and_disabled_users_are_skipped(self):
        names = [_username("expired"), _username("disabled")]
        for name in names:
            _order(name, timedelta(days=30))
        _, send = self._run_limit_pass([
            {"username": names[0], "status": "expired",
             "used_traffic": 95, "data_limit": 100},
            {"username": names[1], "status": "disabled",
             "used_traffic": 95, "data_limit": 100},
        ])
        self.assertEqual(_sends_to_shopper(send), [])


async def _get_order_obj(order_id: int):
    async with SessionLocal() as session:
        return await session.get(Order, order_id)


class ExpiryStatusTest(unittest.TestCase):
    """Expiry reminders are only for active panel subscriptions."""

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(SHOPPER))

    def test_limited_order_gets_no_expiry_reminder(self):
        active_name = _username("expiry-active")
        limited_name = _username("expiry-limited")
        active_id = _order(active_name, timedelta(days=1))
        limited_id = _order(limited_name, timedelta(days=1))

        with patch.object(
            subs, "_active_panel_usernames",
            AsyncMock(return_value={active_name}),
        ):
            ids = {
                order.order_id
                for order in _run(subs._scan_subscriptions_expire_date())
            }

        self.assertIn(active_id, ids)
        self.assertNotIn(limited_id, ids)

    def test_claim_is_released_if_order_is_superseded_before_send(self):
        username = _username("expiry-race")
        order_id = _order(username, timedelta(days=1))
        order_obj = _run(_get_order_obj(order_id))

        original_claim = subs._claim_notification

        async def claim_then_supersede(order_id, kind, cycle):
            claimed = await original_claim(order_id, kind, cycle)
            await db.supersede_orders_for_panel(username, keep_order_id=999999)
            return claimed

        with patch.object(
            subs, "_scan_subscriptions_expire_date",
            AsyncMock(return_value=[order_obj]),
        ), patch.object(
            subs, "_claim_notification", side_effect=claim_then_supersede,
        ), patch.object(
            subs, "_send_telegram_message", AsyncMock(),
        ) as send:
            subs.send_expire_reminder()

        send.assert_not_awaited()
        self.assertEqual(_logs(order_id), [])


class UsageRaceTest(unittest.TestCase):
    """A usage claim must be dropped if the order is renewed mid-scan."""

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(SHOPPER))

    def test_claim_is_released_if_renewal_is_approved_before_send(self):
        username = _username("usage-race")
        old_id = _order(username, timedelta(days=30))
        new_id = _order(
            username, timedelta(days=60),
            approve=False, order_type="renewal",
        )
        new_expiry = datetime.now() + timedelta(days=60)

        original_claim = subs._claim_notification

        async def claim_then_approve(order_id, kind, cycle):
            claimed = await original_claim(order_id, kind, cycle)
            await db.approve_renewal_order(new_id, username, new_expiry)
            return claimed

        with patch.object(
            subs, "_scan_subscriptions_limit_usage",
            AsyncMock(return_value=[{
                "username": username, "status": "active",
                "used_traffic": 95, "data_limit": 100,
            }]),
        ), patch.object(
            subs, "_claim_notification", side_effect=claim_then_approve,
        ), patch.object(
            subs, "_send_telegram_message", AsyncMock(),
        ) as send:
            subs.send_limit_reminder()

        send.assert_not_awaited()
        self.assertEqual(
            [log for log in _logs(old_id)
             if log.kind == NotificationKind.USAGE_90],
            [],
        )


class SchemaUpgradeTest(unittest.TestCase):
    """The prod DB predates `superseded_at`; both the bot and (now) the Celery
    worker must be able to migrate it without failing on a racing duplicate."""

    def test_legacy_orders_table_gains_the_new_column(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = create_engine(f"sqlite:///{path}")
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE orders ("
                    "order_id INTEGER PRIMARY KEY, telegram_id INTEGER, "
                    "plan_id VARCHAR, created_at DATETIME, expire_at DATETIME)"
                )
                db._ensure_order_columns(conn)
                db._ensure_order_columns(conn)  # second starter: no-op
                columns = {
                    row[1]
                    for row in conn.exec_driver_sql(
                        "PRAGMA table_info(orders)"
                    ).fetchall()
                }
            self.assertIn("superseded_at", columns)
            self.assertIn("telegram_username", columns)
        finally:
            engine.dispose()
            os.unlink(path)

    def test_celery_worker_installs_the_schema_hook(self):
        import weakref

        from celery.signals import worker_init

        from app import celery_app as celery_module

        receivers = [
            receiver() if isinstance(receiver, weakref.ReferenceType) else receiver
            for _, receiver in worker_init.receivers
        ]
        self.assertIn(celery_module._ensure_schema, receivers)
        celery_module._ensure_schema()  # up-to-date DB -> no-op


if __name__ == "__main__":
    unittest.main()
