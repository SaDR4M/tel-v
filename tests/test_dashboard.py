"""Read-only web admin dashboard (auth, reports, escaping, live endpoint)."""
import base64
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import dashboard
from config import settings

GB = 1024 ** 3
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}


def _sales_row(**overrides):
    row = {
        "telegram_id": 111,
        "telegram_username": "ali",
        "plan_id": "plan_1m_10gb",
        "coupon_code": None,
        "final_price": 50000,
        "created_at": datetime(2026, 10, 1, 10, 0),
    }
    row.update(overrides)
    return row


def _order_row(**overrides):
    row = {
        "order_id": 7,
        "telegram_id": 111,
        "telegram_username": "ali",
        "plan_id": "plan_1m_10gb",
        "requested_username": "ali_10",
        "order_type": "purchase",
        "status": "active",
        "panel_username": "ali_10",
        "target_panel_username": None,
        "manual_link": None,
        "coupon_code": None,
        "final_price": 50000,
        "created_at": datetime(2026, 10, 1, 10, 0),
        "expire_at": datetime(2026, 11, 1, 10, 0),
    }
    row.update(overrides)
    return row


class DashboardConfigTest(unittest.TestCase):
    def test_create_app_requires_credentials(self):
        with patch.object(settings, "DASHBOARD_USERNAME", ""), \
             patch.object(settings, "DASHBOARD_PASSWORD", ""):
            with self.assertRaises(RuntimeError):
                dashboard.create_app()


class DashboardHttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._creds = (
            patch.object(settings, "DASHBOARD_USERNAME", "admin"),
            patch.object(settings, "DASHBOARD_PASSWORD", "secret"),
        )
        for patcher in self._creds:
            patcher.start()
        self.client = TestClient(TestServer(dashboard.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for patcher in self._creds:
            patcher.stop()

    async def test_requires_basic_auth(self):
        self.assertEqual((await self.client.get("/")).status, 401)
        wrong = {
            "Authorization": "Basic "
            + base64.b64encode(b"admin:nope").decode()
        }
        self.assertEqual((await self.client.get("/", headers=wrong)).status, 401)
        self.assertEqual((await self.client.get("/api/live")).status, 401)

    async def test_home_renders_sales_summary_and_live_widget(self):
        with patch.object(
            dashboard.db, "get_sales_data", AsyncMock(return_value=[_sales_row()])
        ):
            response = await self.client.get("/", headers=AUTH)
            text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("گزارش فروش کل", text)
        self.assertIn('id="live-total"', text)
        self.assertIn("50,000", text)

    async def test_orders_page_escapes_buyer_controlled_fields(self):
        row = _order_row(
            telegram_username="<script>alert(1)</script>",
            requested_username="<img src=x onerror=alert(2)>",
        )
        with patch.object(
            dashboard.db, "count_orders", AsyncMock(return_value=1)
        ), patch.object(
            dashboard.db, "get_orders_page", AsyncMock(return_value=[row])
        ):
            response = await self.client.get("/orders", headers=AUTH)
            text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertNotIn("<img src=x onerror=alert(2)>", text)

    async def test_buyers_page_escapes_usernames(self):
        with patch.object(
            dashboard.db,
            "get_sales_data",
            AsyncMock(
                return_value=[
                    _sales_row(telegram_username="<script>alert(1)</script>")
                ]
            ),
        ):
            response = await self.client.get("/buyers", headers=AUTH)
            text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn("<script>alert(1)</script>", text)

    async def test_panel_page_shows_subscription_usage(self):
        with patch.object(
            dashboard.panel,
            "get_allocation",
            AsyncMock(return_value={"created": 2 * GB, "used": GB}),
        ), patch.object(
            dashboard.panel,
            "get_system_stats",
            AsyncMock(return_value={"total_user": 1, "online_users": 1}),
        ), patch.object(
            dashboard.panel,
            "get_overview",
            AsyncMock(
                return_value={
                    "subscription": {
                        "traffic_limit": 2 * GB,
                        "traffic_used": GB,
                    }
                }
            ),
        ), patch.object(
            dashboard.panel,
            "get_users_list",
            AsyncMock(
                return_value=[
                    {"username": "ali_10", "data_limit": GB, "used_traffic": 0}
                ]
            ),
        ), patch.object(
            dashboard.db, "get_all_panel_usernames", AsyncMock(return_value=["ali_10"])
        ):
            response = await self.client.get("/panel", headers=AUTH)
            text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("وضعیت داده پنل", text)
        self.assertIn("1.00 گیگابایت", text)
        self.assertIn("2.00 گیگابایت", text)

    async def test_panel_page_handles_unreachable_panel(self):
        with patch.object(
            dashboard.panel, "get_allocation", AsyncMock(side_effect=OSError("down"))
        ):
            response = await self.client.get("/panel", headers=AUTH)
            text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("اتصال به پنل برقرار نشد", text)

    @unittest.skipIf(
        dashboard.repository is None, "analytics module is not deployed"
    )
    async def test_live_api_returns_counts(self):
        payload = {
            "seconds": 60,
            "total": 3,
            "by_type": [{"event_type": "click", "count": 3}],
        }
        with patch.object(
            dashboard.repository, "recent_counts", AsyncMock(return_value=payload)
        ):
            response = await self.client.get("/api/live", headers=AUTH)
            data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(data, {"ok": True, **payload})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @unittest.skipIf(
        dashboard.repository is None, "analytics module is not deployed"
    )
    async def test_live_api_is_graceful_without_database(self):
        with patch.object(
            dashboard.repository,
            "recent_counts",
            AsyncMock(side_effect=RuntimeError("no postgres")),
        ):
            response = await self.client.get("/api/live", headers=AUTH)
            data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["by_type"], [])


if __name__ == "__main__":
    unittest.main()
