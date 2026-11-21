"""Subscription tiers: VIP (panel-provisioned) vs Unlimited (API-provisioned)."""
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
        self.assertNotIn("tierplans:eco", cbs)

    def test_economy_tier_is_removed(self):
        self.assertFalse(any(p.get("tier") == "eco" for p in settings.PLANS))
        self.assertNotIn("eco", keyboards.TIER_INFO)

    def test_plans_menu_is_tier_scoped_without_manual_hints(self):
        for tier in ("vip", "unlimited"):
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

    def test_unlimited_tier_has_one_user_based_plan(self):
        plans = {p["id"]: p for p in settings.PLANS
                 if p.get("tier") == "unlimited"}
        plan = plans["plan_u_300gb"]
        self.assertEqual(int(plan["price"]), 150000)
        self.assertEqual(plan["data_limit_bytes"], 0)
        self.assertEqual(plan["provider"], "dante_api")
        self.assertNotIn("plan_u_400gb", plans)
        self.assertNotIn("plan_u_unlimited", plans)

    def test_tier_info_marketing_copy(self):
        # Every offered tier has an info text; the unlimited one presents the
        # current location caption (caps are NOT shown per marketing decision),
        # plus the overuse quality warning.
        tiers_present = {p.get("tier", "vip") for p in settings.PLANS}
        for tier in tiers_present:
            self.assertIn(tier, keyboards.TIER_INFO)
        info = keyboards.TIER_INFO["unlimited"]
        self.assertIn("ONE LOCATION", info)
        self.assertIn("DE", info)
        self.assertNotIn("3 کاربره", info)
        self.assertNotIn("گیگ", info)   # caps deliberately hidden
        self.assertIn("افت کیفیت", info)

    def test_renewal_plans_are_provider_scoped(self):
        vip = {
            b.callback_data for row in keyboards.renewal_plans_menu("vip_panel").inline_keyboard
            for b in row if b.callback_data.startswith("renew_plan:")
        }
        unlimited = {
            b.callback_data for row in keyboards.renewal_plans_menu("dante_api").inline_keyboard
            for b in row if b.callback_data.startswith("renew_plan:")
        }
        self.assertIn("renew_plan:plan_1m_10gb", vip)
        self.assertNotIn("renew_plan:plan_u_300gb", vip)
        self.assertEqual(unlimited, {"renew_plan:plan_u_300gb"})


def _approve_callback(order_id):
    message = SimpleNamespace(caption="رسید پرداخت", edit_caption=AsyncMock())
    return SimpleNamespace(
        from_user=SimpleNamespace(id=_admin_id(), first_name="Admin"),
        data=f"admin_approve:{order_id}",
        message=message,
        answer=AsyncMock(),
    )


class UnlimitedApproveTest(unittest.TestCase):
    def _approve_unlimited(self, extended=False):
        plan = settings.get_plan("plan_u_300gb")
        order = {
            "order_id": 501, "telegram_id": 42, "plan_id": plan["id"],
            "status": "pending", "order_type": "purchase",
            "requested_username": "user",
        }
        callback = _approve_callback(501)
        bot = SimpleNamespace(send_message=AsyncMock())
        result = SimpleNamespace(
            extended=extended,
            user=SimpleNamespace(
                subscription_url="https://sub.example.com/dante"
            ),
        )
        with patch("database.get_order", AsyncMock(return_value=order)), \
             patch.object(
                 admin_review, "approve_purchase",
                 AsyncMock(return_value=result),
             ) as approve:
            _run(admin_review.cb_admin_approve(callback, bot))
        return plan, order, bot, callback, approve

    def test_unlimited_approve_provisions_and_sends_link(self):
        _, order, bot, callback, approve = self._approve_unlimited()
        approve.assert_awaited_once()
        self.assertEqual(approve.await_args.args[1]["id"], "plan_u_300gb")
        buyer_text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("https://sub.example.com/dante", buyer_text)
        self.assertIn("آماده است", buyer_text)
        caption = callback.message.edit_caption.await_args.kwargs["caption"]
        self.assertIn("خودکار", caption)

    def test_unlimited_approve_reports_extension(self):
        _, _, bot, _, _ = self._approve_unlimited(extended=True)
        buyer_text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("تمدید شد", buyer_text)

    def test_unlimited_renewal_uses_provider_service(self):
        plan = settings.get_plan("plan_u_300gb")
        order = {
            "order_id": 506, "telegram_id": 42, "plan_id": plan["id"],
            "status": "pending", "order_type": "renewal",
            "target_provider": "dante_api",
            "target_provider_user_id": "dante-11",
        }
        callback = _approve_callback(506)
        bot = SimpleNamespace(send_message=AsyncMock())
        result = SimpleNamespace(
            extended=True,
            user=SimpleNamespace(
                subscription_url="https://sub.example.com/same"
            ),
        )
        with patch("database.get_order", AsyncMock(return_value=order)), \
             patch.object(
                 admin_review, "approve_renewal",
                 AsyncMock(return_value=result),
             ) as renew, \
             patch.object(
                 admin_review.panel, "renew_user", AsyncMock()
             ) as panel_renew:
            _run(admin_review.cb_admin_approve(callback, bot))

        renew.assert_awaited_once()
        panel_renew.assert_not_awaited()
        buyer_text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("تمدید با موفقیت", buyer_text)
        self.assertIn("https://sub.example.com/same", buyer_text)

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
        order = {"order_id": 503, "telegram_id": 42, "plan_id": "plan_u_300gb",
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
    """VIP panel usernames and Unlimited provider accounts stay in separate
    renewal scopes; a VIP account is only ever extended by VIP plans."""

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def test_manual_orders_never_reachable_via_renewal(self):
        shopper = 999002
        try:
            manual = _run(db.create_order(
                telegram_id=shopper, plan_id="plan_u_300gb",
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

    def test_empty_renewal_list_offers_new_purchase(self):
        import handlers.renewal as renewal
        with patch.object(
            renewal, "_fetch_renewable_subs", AsyncMock(return_value=[])
        ):
            text, kb = _run(renewal._show_renewal_list(999002))
        self.assertIn("سرویس جدید", text)
        self.assertTrue(kb.inline_keyboard)


class ManualLinkDbRoundTripTest(unittest.TestCase):
    """New DB helpers against the real dev DB (own rows, cleaned up)."""
    SHOP_TG = 999001

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def test_manual_link_roundtrip(self):
        oid = _run(db.create_order(
            telegram_id=self.SHOP_TG,
            plan_id="plan_u_300gb",
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
