"""Audience selection for admin broadcasts."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from sqlalchemy import delete, select

import database as db
from models import User
from database import SessionLocal


def _run(coro):
    return asyncio.run(coro)


class BroadcastAudienceTest(unittest.TestCase):
    ACTIVE = 999401
    APPROVED = 999402
    EXPIRED = 999403
    OTHER_PLAN = 999404
    USERNAME = 999405
    ALL = (ACTIVE, APPROVED, EXPIRED, OTHER_PLAN, USERNAME)

    @classmethod
    def setUpClass(cls):
        _run(db.init_db())

    def tearDown(self):
        _run(db.delete_test_orders(self.ACTIVE))
        _run(db.delete_test_orders(self.APPROVED))
        _run(db.delete_test_orders(self.EXPIRED))
        _run(db.delete_test_orders(self.OTHER_PLAN))

        async def cleanup_user():
            async with SessionLocal() as session:
                await session.execute(
                    delete(User).where(User.telegram_id == self.USERNAME)
                )
                await session.commit()

        _run(cleanup_user())

    def _order(self, telegram_id, *, plan="plan_u_300gb", status="pending",
               days=10):
        order_id = _run(db.create_order(
            telegram_id=telegram_id,
            plan_id=plan,
            requested_username="broadcast-test",
            receipt_file_id="test",
            expire_at=datetime.now() + timedelta(days=days),
        ))
        _run(db.set_order_status(order_id, status))
        return order_id

    def test_active_plan_and_all_audiences(self):
        self._order(self.ACTIVE, status="active")
        self._order(self.APPROVED, status="approved")
        self._order(self.EXPIRED, status="active", days=-1)
        self._order(self.OTHER_PLAN, plan="plan_1m_10gb", status="active")

        recipients = set(
            _run(db.get_active_telegram_ids_for_plan("plan_u_300gb"))
        )
        self.assertTrue({self.ACTIVE, self.APPROVED} <= recipients)
        self.assertNotIn(self.EXPIRED, recipients)
        self.assertNotIn(self.OTHER_PLAN, recipients)

        counts = _run(db.count_active_users_by_plan())
        self.assertGreaterEqual(counts.get("plan_u_300gb", 0), 2)
        self.assertGreaterEqual(counts.get("plan_1m_10gb", 0), 1)

        all_ids = set(_run(db.get_all_telegram_ids()))
        self.assertTrue({self.ACTIVE, self.APPROVED, self.OTHER_PLAN} <= all_ids)

    def test_username_lookup(self):
        _run(db.upsert_user(self.USERNAME, "BroadcastUser"))
        self.assertEqual(
            _run(db.find_telegram_id_by_username("@broadcastuser")),
            self.USERNAME,
        )
        self.assertIsNone(
            _run(db.find_telegram_id_by_username("@missing_broadcast_user"))
        )


if __name__ == "__main__":
    unittest.main()
