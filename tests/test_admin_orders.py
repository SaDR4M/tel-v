"""Admin orders dashboard: pagination maths, full-data rendering, guards."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

import database as db
from config import settings
from handlers import admin as admin_mod


def _run(coro):
    return asyncio.run(coro)


class OrderBlockTest(unittest.TestCase):
    def _row(self, **over):
        row = {
            "order_id": 12,
            "telegram_id": 1173916658,
            "telegram_username": "sadra",
            "plan_id": "plan_1m_75gb",
            "requested_username": "علی",
            "order_type": "purchase",
            "status": "pending",
            "panel_username": None,
            "target_panel_username": None,
            "manual_link": None,
            "coupon_code": None,
            "final_price": None,
            "created_at": datetime(2025, 5, 1, 13, 30),
            "expire_at": datetime(2025, 6, 1, 13, 30),
        }
        row.update(over)
        return row

    def test_block_shows_every_admin_field(self):
        text = admin_mod._order_block(self._row())
        self.assertIn("#12", text)
        self.assertIn("@sadra", text)
        self.assertIn("1173916658", text)
        plan = settings.get_plan("plan_1m_75gb")
        self.assertIn(plan["name"], text)
        self.assertIn(f"{int(plan['price']):,}", text)  # list price when unpaid
        self.assertIn("خرید", text)
        self.assertIn("در انتظار بررسی", text)
        self.assertIn("علی", text)
        self.assertIn("2025/05/01 13:30", text)          # created_at
        self.assertIn("2025/06/01 13:30", text)          # expire_at

    def test_block_prefers_final_price_and_marks_discount(self):
        text = admin_mod._order_block(
            self._row(coupon_code="FRIENDS", final_price=300000)
        )
        self.assertIn("300,000", text)
        self.assertIn(f"از {int(settings.get_plan('plan_1m_75gb')['price']):,}", text)
        self.assertIn("FRIENDS", text)

    def test_block_delivery_variants(self):
        panel = admin_mod._order_block(
            self._row(status="active", panel_username="ali_1173916658")
        )
        self.assertIn("حساب پنل", panel)
        self.assertIn("ali_1173916658", panel)
        self.assertIn("فعال", panel)

        manual = admin_mod._order_block(
            self._row(plan_id="plan_u_300gb", manual_link="https://sub/x")
        )
        self.assertIn("لینک دستی", manual)

        renewal = admin_mod._order_block(
            self._row(order_type="renewal", target_panel_username="bob_1")
        )
        self.assertIn("تمدید", renewal)
        self.assertIn("bob_1", renewal)

    def test_status_enum_and_unknown_values_render_safely(self):
        from models import OrderStatus
        text = admin_mod._order_block(self._row(status=OrderStatus.EXPIRED))
        self.assertIn("منقضی", text)
        weird = admin_mod._order_block(self._row(status="mystery"))
        self.assertIn("mystery", weird)


class OrdersMenuTest(unittest.TestCase):
    def _callbacks(self, kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_first_page_has_no_newer_button(self):
        cbs = self._callbacks(admin_mod._orders_menu(page=0, pages=3))
        self.assertNotIn("admin:orders:-1", cbs)
        self.assertIn("admin:orders:1", cbs)
        self.assertIn("admin:home", cbs)

    def test_last_page_has_no_older_button(self):
        cbs = self._callbacks(admin_mod._orders_menu(page=2, pages=3))
        self.assertIn("admin:orders:1", cbs)
        self.assertNotIn("admin:orders:3", cbs)

    def test_single_page_has_only_back(self):
        cbs = self._callbacks(admin_mod._orders_menu(page=0, pages=1))
        self.assertEqual(cbs, ["admin:home"])

    def test_text_header_shows_page_numbers(self):
        text = admin_mod._orders_text([], page=1, pages=4)
        self.assertIn("2", text)
        self.assertIn("4", text)
        self.assertIn("سفارشی ثبت نشده", text)


class OrdersPaginationDbTest(unittest.TestCase):
    """Real dev DB: create 12 throwaway orders, then page through them."""

    SHOP_TG = 999003

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())
        _run(db.delete_test_orders(cls.SHOP_TG))
        for i in range(12):
            _run(db.create_order(
                telegram_id=cls.SHOP_TG,
                plan_id="plan_1m_10gb",
                requested_username=f"pager-{i}",
                receipt_file_id="t",
                expire_at=datetime.now() + timedelta(days=30),
            ))

    @classmethod
    def tearDownClass(cls):
        _run(db.delete_test_orders(cls.SHOP_TG))

    def test_count_includes_seeded_rows(self):
        self.assertGreaterEqual(_run(db.count_orders()), 12)

    def test_pages_are_disjoint_newest_first(self):
        local_total = 12
        first = _run(db.get_orders_page(0, 5))
        second = _run(db.get_orders_page(5, 5))
        third = _run(db.get_orders_page(10, 5))
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertGreaterEqual(len(third), 2)

        seen = [r["order_id"] for r in first + second + third]
        self.assertEqual(len(seen), len(set(seen)))          # no overlap
        self.assertEqual(seen, sorted(seen, reverse=True))   # newest first
        # Rows carry the full dashboard payload.
        for key in ("order_id", "telegram_id", "plan_id", "status",
                    "order_type", "created_at", "expire_at",
                    "requested_username", "final_price", "coupon_code"):
            self.assertIn(key, first[0])


class OrdersDashboardHandlerTest(unittest.TestCase):
    def _callback(self, data):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=next(iter(settings.ADMIN_USER_IDS))),
            data=data,
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )

    def test_non_admin_gets_nothing(self):
        cb = self._callback("admin:orders:0")
        cb.from_user.id = -1
        with patch("database.get_orders_page", AsyncMock()) as m_page:
            _run(admin_mod.cb_admin_orders(cb))
        m_page.assert_not_awaited()
        cb.answer.assert_awaited_once()

    def test_out_of_range_page_is_clamped(self):
        cb = self._callback("admin:orders:999")
        rows = [{"order_id": 1, "telegram_id": 2, "plan_id": "plan_1m_10gb",
                 "status": "pending", "order_type": "purchase",
                 "requested_username": "x", "panel_username": None,
                 "target_panel_username": None, "manual_link": None,
                 "coupon_code": None, "final_price": None,
                 "telegram_username": None, "created_at": None, "expire_at": None}]
        with patch.object(admin_mod.db, "count_orders", AsyncMock(return_value=12)), \
             patch.object(admin_mod.db, "get_orders_page", AsyncMock(return_value=rows)) as m_page:
            _run(admin_mod.cb_admin_orders(cb))
        m_page.assert_awaited_once_with(offset=10, limit=5)  # page clamped to 2
        cb.answer.assert_awaited_once()
