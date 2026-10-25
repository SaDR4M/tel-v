"""Orders snapshot the buyer's Telegram username when one exists."""
import ast
import asyncio
import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

import database as db
from handlers import test_plan


def _run(coro):
    return asyncio.run(coro)


class OrderUsernameDbTest(unittest.TestCase):
    SHOP_TG = 999010

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    @classmethod
    def tearDownClass(cls):
        _run(db.delete_test_orders(cls.SHOP_TG))

    def _create(self, telegram_username):
        return _run(db.create_order(
            telegram_id=self.SHOP_TG,
            telegram_username=telegram_username,
            plan_id="plan_1m_10gb",
            requested_username="username-test",
            receipt_file_id="t",
            expire_at=datetime.now() + timedelta(days=1),
        ))

    def test_username_is_saved_when_present(self):
        order_id = self._create("alice")
        order = _run(db.get_order(order_id))
        self.assertEqual(order["telegram_username"], "alice")

    def test_no_username_is_stored_when_absent(self):
        order_id = self._create(None)
        order = _run(db.get_order(order_id))
        self.assertIsNone(order["telegram_username"])

    def test_blank_username_is_normalised_to_none(self):
        order_id = self._create("   ")
        order = _run(db.get_order(order_id))
        self.assertIsNone(order["telegram_username"])

    def test_admin_orders_page_exposes_the_snapshot(self):
        order_id = self._create("alice")
        rows = _run(db.get_orders_page(0, 1000))
        row = next(r for r in rows if r["order_id"] == order_id)
        self.assertEqual(row["telegram_username"], "alice")


class TestPlanUsernameTest(unittest.TestCase):
    def test_test_plan_order_stores_the_username(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=999011, username="tester"),
            answer=AsyncMock(),
        )
        with patch.object(test_plan.db, "has_used_plan", AsyncMock(return_value=False)), \
             patch.object(test_plan.db, "create_order", AsyncMock(return_value=1)) as create, \
             patch.object(
                 test_plan.panel,
                 "create_user",
                 AsyncMock(return_value={"subscription_url": "https://sub/x"}),
             ), \
             patch.object(test_plan.db, "link_panel_account", AsyncMock()), \
             patch.object(test_plan.db, "set_order_status", AsyncMock()):
            _run(test_plan.cb_apply_test(message))
        self.assertEqual(create.await_args.kwargs["telegram_username"], "tester")


class CreateOrderCallSitesTest(unittest.TestCase):
    def test_every_handler_order_creation_passes_the_username(self):
        offenders = []
        root = pathlib.Path(__file__).resolve().parent.parent / "handlers"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "create_order"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "db"
                ):
                    keywords = {kw.arg for kw in node.keywords}
                    if "telegram_username" not in keywords:
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "create_order callers missing telegram_username: " + "; ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
