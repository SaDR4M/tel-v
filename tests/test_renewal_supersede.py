"""A renewal reuses the panel account, so the order row it replaces must stop
producing expiry/usage reminders — only the newest order stays current."""
import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database as db
import handlers.admin_review as admin_review
from config import settings
from tasks import subs

SHOPPER = 999005  # throwaway buyer, erased in tearDown


def _run(coro):
    return asyncio.run(coro)


def _admin_id():
    return next(iter(settings.ADMIN_USER_IDS))


def _username(tag: str) -> str:
    return f"renew_{tag}_{SHOPPER}"


def _old_order(username: str, expires_in: timedelta) -> int:
    order_id = _run(db.create_order(
        telegram_id=SHOPPER,
        plan_id="plan_1m_10gb",
        requested_username="renew-old",
        receipt_file_id="t",
        expire_at=datetime.now() + expires_in,
    ))
    _run(db.set_order_status(order_id, "approved", username))
    return order_id


def _renewal_order(
    username: str,
    expires_in: timedelta = timedelta(days=30),
    approve: bool = True,
) -> int:
    order_id = _run(db.create_order(
        telegram_id=SHOPPER,
        plan_id="plan_1m_10gb",
        requested_username="renew-new",
        receipt_file_id="t",
        expire_at=datetime.now() + expires_in,
        order_type="renewal",
        target_panel_username=username,
    ))
    if approve:
        # Approved renewals adopt the reused panel username; that is when they
        # become the current order for the account.
        _run(db.set_order_status(order_id, "approved", username))
    return order_id


class SupersedeHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(SHOPPER))

    def test_only_the_kept_order_stays_current(self):
        username = _username("helper")
        old_id = _old_order(username, timedelta(days=5))
        new_id = _renewal_order(username)

        self.assertEqual(
            _run(db.supersede_orders_for_panel(username, keep_order_id=new_id)), 1
        )
        self.assertIsNotNone(_run(db.get_order(old_id))["superseded_at"])
        self.assertIsNone(_run(db.get_order(new_id))["superseded_at"])

        # Re-approving an even newer renewal re-arms the marker for the previous
        # one, and already-marked rows are left alone.
        newest_id = _renewal_order(username)
        self.assertEqual(
            _run(db.supersede_orders_for_panel(username, keep_order_id=newest_id)), 1
        )
        self.assertEqual(
            _run(db.supersede_orders_for_panel(username, keep_order_id=newest_id)), 0
        )


class ReminderScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(SHOPPER))

    def test_superseded_order_gets_no_expiry_or_usage_reminder(self):
        username = _username("scan")
        # Old row expires LAST so only the supersede filter (not the "newest
        # expiry" ordering) can keep it out of the reminders.
        old_id = _old_order(username, timedelta(days=1, hours=12))
        new_id = _renewal_order(username, expires_in=timedelta(days=1))

        ids = {o.order_id for o in _run(subs._scan_subscriptions_expire_date())}
        self.assertIn(old_id, ids)  # would have been reminded before the fix

        _run(db.supersede_orders_for_panel(username, keep_order_id=new_id))

        ids = {o.order_id for o in _run(subs._scan_subscriptions_expire_date())}
        self.assertNotIn(old_id, ids)
        self.assertIn(new_id, ids)

        fetched = _run(subs._fetch_order(username))
        self.assertEqual(fetched.order_id, new_id)

        # mysub / «تمدید» must show the account once, via the current order.
        visible = {o["order_id"] for o in _run(db.get_active_orders(SHOPPER))}
        self.assertNotIn(old_id, visible)
        self.assertIn(new_id, visible)


class RenewalApproveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(SHOPPER))

    def test_approval_retires_previous_order_and_aligns_expiry(self):
        username = _username("approve")
        old_id = _old_order(username, timedelta(hours=3))
        renewal_id = _renewal_order(username, approve=False)

        expire_ts = int((datetime.now() + timedelta(days=30)).timestamp())
        bot = SimpleNamespace(send_message=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=_admin_id(), first_name="Admin"),
            data=f"admin_approve:{renewal_id}",
            message=SimpleNamespace(caption="رسید", edit_caption=AsyncMock()),
            answer=AsyncMock(),
        )

        with patch.object(
            admin_review.panel, "renew_user",
            AsyncMock(return_value={
                "expire": expire_ts,
                "subscription_url": "https://sub/unchanged",
            }),
        ) as renew, patch("database.link_panel_account", AsyncMock()):
            _run(admin_review.cb_admin_approve(callback, bot))

        renew.assert_awaited_once()
        self.assertEqual(renew.await_args.kwargs["username"], username)

        old = _run(db.get_order(old_id))
        self.assertIsNotNone(old["superseded_at"])

        renewed = _run(db.get_order(renewal_id))
        self.assertEqual(renewed["status"], "approved")
        self.assertEqual(renewed["panel_username"], username)
        self.assertIsNone(renewed["superseded_at"])
        self.assertAlmostEqual(
            renewed["expire_at"].timestamp(), expire_ts, delta=2
        )

        bot.send_message.assert_awaited_once()
        self.assertIn("تمدید", bot.send_message.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
