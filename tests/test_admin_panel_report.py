import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from handlers.admin import _panel_report, _admin_menu

GiB = 1024 ** 3


def _allocation(created: int, used: int) -> dict:
    return {"created": created, "used": used, "unlimited": 0}


def _system() -> dict:
    return {
        "total_user": 43, "online_users": 22,
        "users_active": 28, "users_on_hold": 0, "users_disabled": 1,
        "users_expired": 6, "users_limited": 8,
    }


def _overview(limit: int, used: int) -> dict:
    return {"subscription": {"traffic_limit": limit, "traffic_used": used}}


def _user(name: str, limit: int, used: int) -> dict:
    return {"username": name, "data_limit": limit, "used_traffic": used}


class PanelReportTest(unittest.TestCase):
    def test_reconciles_panel_vs_orders(self):
        # panel: 100 GiB assigned, 60 used; one of two users is ours
        allocation = _allocation(100 * GiB, 60 * GiB)
        users = [_user("mine_1", 40 * GiB, 30 * GiB),
                 _user("manual_2", 60 * GiB, 30 * GiB)]
        txt = _panel_report(allocation, _system(),
                            _overview(2000 * GiB, 900 * GiB),
                            users, ["mine_1"])
        self.assertIn("واگذارشده: <b>100.00 گیگابایت</b>", txt)
        self.assertIn("مصرف‌شده: <b>60.00 گیگابایت</b>", txt)
        # orders side sees only the tracked user
        self.assertIn("واگذارشده: <b>40.00 گیگابایت</b> (1 حساب)", txt)
        self.assertIn("مصرف‌شده: <b>30.00 گیگابایت</b>", txt)
        # differences: panel − orders = the untracked user
        self.assertIn("واگذاری: <b>60.00 گیگابایت</b> خارج از ربات (1 کاربر)", txt)
        self.assertIn("مصرف: <b>30.00 گیگابایت</b> خارج از ربات", txt)
        # provider package context
        self.assertIn("1,100.00 باقی از 2,000.00 گیگابایت", txt)
        # status breakdown
        self.assertIn("43 (28 فعال / 8 محدود / 6 منقضی / 1 غیرفعال)", txt)

    def test_orders_side_zero_when_no_tracked_orders(self):
        allocation = _allocation(50 * GiB, 20 * GiB)
        txt = _panel_report(allocation, _system(), _overview(0, 0),
                            [_user("stranger", 50 * GiB, 20 * GiB)], [])
        self.assertIn("واگذارشده: <b>0.00 گیگابایت</b> (0 حساب)", txt)
        self.assertIn("واگذاری: <b>50.00 گیگابایت</b> خارج از ربات (1 کاربر)", txt)
        self.assertNotIn("حساب ثبت‌شده که در پنل نیست", txt)

    def test_missing_tracked_account_is_reported(self):
        allocation = _allocation(10 * GiB, 0)
        txt = _panel_report(allocation, _system(), _overview(0, 0),
                            [], ["ghost_1"])
        self.assertIn("حساب ثبت‌شده که در پنل نیست: 1", txt)
        self.assertIn("(0 حساب)", txt)

    def test_unlimited_allocation_field_is_ignored_safely(self):
        allocation = {"created": 0, "used": 0, "unlimited": 3}
        txt = _panel_report(allocation, _system(), _overview(0, 0), [], [])
        self.assertIn("واگذارشده: <b>0.00 گیگابایت</b>", txt)

    def test_menu_has_panel_report(self):
        cb_data = [b.callback_data for row in _admin_menu().inline_keyboard
                   for b in row]
        self.assertIn("admin:panel", cb_data)

    def test_callback_gated_for_non_admin(self):
        from types import SimpleNamespace
        import asyncio
        from handlers import admin as A

        # non-admin: the panel API must not be touched at all
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=999999),
            message=None,
            answer=AsyncMock(),
        )
        with patch.object(A.panel, "get_allocation",
                          new_callable=AsyncMock) as m:
            asyncio.run(A.cb_admin_panel(cb))
            m.assert_not_called()
        cb.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
