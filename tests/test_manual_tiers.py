"""New subscription tiers: VIP (auto-provisioned) vs Eco/Unlimited (created
manually by the admin, link delivered by replying to the order receipt)."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

import handlers.admin_review as admin_review
import handlers.mysub as mysub
import database as db
from config import settings
from handlers import keyboards


def _run(coro):
    return asyncio.run(coro)


def _admin_id():
    return next(iter(settings.ADMIN_USER_IDS))


class TierMenusTest(unittest.TestCase):
    def test_tiers_menu_lists_all_present_tiers(self):
        tiers_present = {p.get("tier", "vip") for p in settings.PLANS}
        kb = keyboards.tiers_menu()
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        for tier in tiers_present:
            self.assertIn(f"tierplans:{tier}", cbs)
        self.assertIn("menu:home", cbs)

    def test_plans_menu_is_tier_scoped_without_manual_hints(self):
        for tier in ("vip", "eco", "unlimited"):
            kb = keyboards.plans_menu(tier)
            buys = [b for row in kb.inline_keyboard for b in row
                    if b.callback_data.startswith("buy:")]
            self.assertTrue(buys, f"tier {tier} must show its plans")
            for b in buys:
                plan = settings.get_plan(b.callback_data.split(":", 1)[1])
                self.assertEqual(plan.get("tier", "vip"), tier)
            # Internal delivery mechanics must never surface to buyers.
            self.assertFalse(
                any("دستی" in b.text for row in kb.inline_keyboard for b in row))

    def test_unlimited_tier_has_two_user_based_plans(self):
        sums = {p["id"]: (int(p["price"]), p["data_limit_bytes"])
                for p in settings.PLANS if p.get("tier") == "unlimited"}
        self.assertEqual(sums.get("plan_u_300gb"), (750000, 300 * 1024**3))
        self.assertEqual(sums.get("plan_u_400gb"), (850000, 400 * 1024**3))
        self.assertNotIn("plan_u_unlimited", sums)  # replaced pair, no stale id

    def test_tier_info_marketing_copy(self):
        # Every offered tier has an info text, and the unlimited one spells
        # out both user counts and the overuse quality warning.
        tiers_present = {p.get("tier", "vip") for p in settings.PLANS}
        for tier in tiers_present:
            self.assertIn(tier, keyboards.TIER_INFO)
        info = keyboards.TIER_INFO["unlimited"]
        self.assertIn("2 کاربر", info)
        self.assertIn("3 کاربر", info)
        self.assertIn("300 گیگ", info)
        self.assertIn("400 گیگ", info)
        self.assertIn("افت کیفیت", info)

    def test_renewal_plans_menu_vip_only(self):
        kb = keyboards.renewal_plans_menu()
        for row in kb.inline_keyboard:
            for b in row:
                if b.callback_data.startswith("buy:"):
                    plan = settings.get_plan(b.callback_data.split(":", 1)[1])
                    self.assertEqual(plan.get("tier", "vip"), "vip")


def _approve_callback(order_id):
    message = SimpleNamespace(caption="رسید پرداخت", edit_caption=AsyncMock())
    return SimpleNamespace(
        from_user=SimpleNamespace(id=_admin_id(), first_name="Admin"),
        data=f"admin_approve:{order_id}",
        message=message,
        answer=AsyncMock(),
    )


class ManualApproveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The ORM now knows `manual_link`; the dev DB gets the column via the
        # additive migration that init_db runs at bot startup.
        _run(db.init_db())

    def test_manual_approve_skips_panel_and_asks_for_link(self):
        plan = settings.get_plan("plan_u_300gb")
        order = {"order_id": 501, "telegram_id": 42, "plan_id": plan["id"],
                 "status": "pending", "order_type": "purchase"}
        callback = _approve_callback(501)
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch("database.get_order", AsyncMock(return_value=order)), \
             patch("database.set_order_status", AsyncMock()) as status, \
             patch("database.set_order_expire_at", AsyncMock()) as expire, \
             patch("database.get_panel_username", AsyncMock()) as get_panel, \
             patch.object(admin_review.panel, "create_user", AsyncMock()) as create, \
             patch.object(admin_review.panel, "get_user", AsyncMock()):
            _run(admin_review.cb_admin_approve(callback, bot))

        create.assert_not_awaited()
        get_panel.assert_not_awaited()  # _free_panel_username probing skipped
        self.assertEqual(status.await_args.args[:2], (501, "approved"))
        self.assertEqual(len(status.await_args.args), 2)  # no panel account linked
        self.assertIsInstance(expire.await_args.args[1], datetime)
        buyer_text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("آماده‌سازی", buyer_text)
        caption = callback.message.edit_caption.await_args.kwargs["caption"]
        self.assertIn("ریپلای", caption)

    def test_vip_approve_still_provisions_via_panel(self):
        plan = settings.get_plan("plan_1m_10gb")
        order = {"order_id": 502, "telegram_id": 42, "plan_id": plan["id"],
                 "status": "pending", "order_type": "purchase",
                 "requested_username": "ali"}
        callback = _approve_callback(502)
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch("database.get_order", AsyncMock(return_value=order)), \
             patch("database.set_order_status", AsyncMock()), \
             patch("database.link_panel_account", AsyncMock()), \
             patch.object(admin_review.panel, "get_user", AsyncMock(return_value=None)), \
             patch.object(admin_review.panel, "create_user",
                          AsyncMock(return_value={"subscription_url": "https://sub/x"})) as create:
            _run(admin_review.cb_admin_approve(callback, bot))

        create.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 42)


def _link_message(admin_message_id, text, user_id):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=settings.ADMIN_CHAT_ID),
        reply_to_message=SimpleNamespace(message_id=admin_message_id, caption="رسید پرداخت"),
        text=text,
        reply=AsyncMock(),
    )


class LinkDeliveryTest(unittest.TestCase):
    def test_receive_manual_link_stores_link_and_dms_buyer(self):
        order = {"order_id": 503, "telegram_id": 42, "plan_id": "plan_e_50gb",
                 "status": "approved", "panel_username": None}
        message = _link_message(777, "ایجاد شد 👇 https://sub.example.com/s/abc123", _admin_id())
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_caption=AsyncMock())
        with patch("handlers.admin_review._is_admin", return_value=True), \
             patch("database.get_order_by_admin_message", AsyncMock(return_value=order)), \
             patch("database.set_manual_link", AsyncMock()) as link:
            _run(admin_review.receive_manual_link(message, bot))

        link.assert_awaited_once_with(503, "https://sub.example.com/s/abc123")
        self.assertTrue(bot.edit_message_caption.await_args.kwargs["caption"].endswith("لینک ارسال شد."))
        buyer = bot.send_message.await_args.kwargs
        self.assertEqual(buyer["chat_id"], 42)
        self.assertIn("abc123", buyer["text"])

    def test_receive_manual_link_guards(self):
        # Not an admin -> silent no-op.
        message = _link_message(778, "https://x.io/sub", -1)
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_caption=AsyncMock())
        order = {"order_id": 504, "status": "approved", "panel_username": None}
        with patch("handlers.admin_review._is_admin", return_value=False), \
             patch("database.get_order_by_admin_message", AsyncMock(return_value=order)), \
             patch("database.set_manual_link", AsyncMock()) as link:
            _run(admin_review.receive_manual_link(message, bot))
        link.assert_not_awaited()
        bot.send_message.assert_not_awaited()

        # Non-URL reply -> polite error, nothing stored.
        message = _link_message(778, "salam", _admin_id())
        message.reply = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_caption=AsyncMock())
        with patch("handlers.admin_review._is_admin", return_value=True), \
             patch("database.get_order_by_admin_message", AsyncMock(return_value=order)), \
             patch("database.set_manual_link", AsyncMock()) as link:
            _run(admin_review.receive_manual_link(message, bot))
        link.assert_not_awaited()
        message.reply.assert_awaited_once()

        # Auto-provisioned (VIP) order -> refused, no buyer DM.
        message = _link_message(778, "https://x.io/sub", _admin_id())
        message.reply = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_caption=AsyncMock())
        vip_order = {"order_id": 505, "status": "approved", "panel_username": "sadra_1"}
        with patch("handlers.admin_review._is_admin", return_value=True), \
             patch("database.get_order_by_admin_message", AsyncMock(return_value=vip_order)), \
             patch("database.set_manual_link", AsyncMock()) as link:
            _run(admin_review.receive_manual_link(message, bot))
        link.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        message.reply.assert_awaited_once()


class MysubManualViewTest(unittest.TestCase):
    def test_build_sub_text_manual_variants(self):
        with patch("database.get_order", AsyncMock(return_value={
                "order_id": 1, "panel_username": None,
                "manual_link": "https://sub/x"})):
            text = _run(mysub._build_sub_text(1))
        self.assertIn("https://sub/x", text)
        self.assertNotIn("آماده‌سازی", text)

        with patch("database.get_order", AsyncMock(return_value={
                "order_id": 2, "panel_username": None, "manual_link": None})):
            text = _run(mysub._build_sub_text(2))
        self.assertIn("آماده‌سازی", text)

        with patch("database.get_order", AsyncMock(return_value={
                "order_id": 3, "panel_username": "sadra_1", "manual_link": None,
                "plan_id": "plan_1m_10gb", "status": "approved"})), \
            patch.object(mysub.panel, "get_user", AsyncMock(return_value={
                "expire": 1800000000, "status": "active", "used_traffic": 0,
                "data_limit": 10737418240, "subscription_url": "https://sub/y"})):
            text = _run(mysub._build_sub_text(3))
        self.assertIn("وضعیت", text)
        self.assertIn("https://sub/y", text)


class RenewalScopeTest(unittest.TestCase):
    """Cross-tier renewal must be impossible: eco/unlimited accounts are not
    bot-owned (no panel_username) so they can never be renewed with the bot's
    panel — a VIP account is only ever extended by VIP plans."""

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def test_manual_orders_never_reachable_via_renewal(self):
        shopper = 999002
        try:
            eco = _run(db.create_order(
                telegram_id=shopper, plan_id="plan_e_50gb",
                requested_username="caps-shopper", receipt_file_id="t",
                expire_at=datetime.now() + timedelta(days=30),
            ))
            vip = _run(db.create_order(
                telegram_id=shopper, plan_id="plan_1m_25gb",
                requested_username="caps-shopper", receipt_file_id="t",
                expire_at=datetime.now() + timedelta(days=30),
            ))
            _run(db.set_order_status(vip, "approved", "caps_renewable_me"))
            usernames = _run(db.get_distinct_panel_usernames(shopper))
            self.assertEqual(usernames, ["caps_renewable_me"])
        finally:
            _run(db.delete_test_orders(shopper))

    def test_low_renewable_message_hints_reorder_for_manual_tiers(self):
        import handlers.renewal as renewal
        manual_order = {"order_id": 1, "plan_id": "plan_e_50gb"}
        with patch.object(renewal, "_fetch_renewable_subs", AsyncMock(return_value=[])), \
             patch("database.get_active_orders", AsyncMock(return_value=[manual_order])):
            text, kb = _run(renewal._show_renewal_list(999002))
        self.assertIn("خرید مجدد", text)
        self.assertTrue(len(kb.inline_keyboard), 1)

        with patch.object(renewal, "_fetch_renewable_subs", AsyncMock(return_value=[])), \
             patch("database.get_active_orders", AsyncMock(return_value=[])):
            text, _ = _run(renewal._show_renewal_list(999002))
        self.assertNotIn("اکونومی", text)


class ManualLinkDbRoundTripTest(unittest.TestCase):
    """New DB helpers against the real dev DB (own rows, cleaned up)."""
    SHOP_TG = 999001

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def test_manual_link_roundtrip(self):
        oid = _run(db.create_order(
            telegram_id=self.SHOP_TG,
            plan_id="plan_e_50gb",
            requested_username="manuallink-tester",
            receipt_file_id="test-file",
            expire_at=datetime.now() + timedelta(days=30),
        ))
        try:
            _run(db.set_order_admin_message(oid, 424242))
            found = _run(db.get_order_by_admin_message(424242))
            self.assertIsNotNone(found)
            self.assertEqual(found["order_id"], oid)
            _run(db.set_manual_link(oid, "https://sub/delivered"))
            _run(db.set_order_expire_at(oid, datetime.now() + timedelta(days=1)))
            order = _run(db.get_order(oid))
            self.assertEqual(order["manual_link"], "https://sub/delivered")
            self.assertEqual(order["status"], "pending")
        finally:
            _run(db.delete_test_orders(self.SHOP_TG))
