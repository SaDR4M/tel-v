"""Coupon pricing, including fixed per-plan prices for manual plans."""
import sys
import unittest

sys.path.insert(0, ".")

from config import settings


class CouponPriceTest(unittest.TestCase):
    def test_friends_has_fixed_price_for_unlimited(self):
        plan = settings.get_plan("plan_u_300gb")
        coupon = settings.get_coupon("FRIENDS")
        self.assertEqual(settings.coupon_price(plan, coupon), 125000)

    def test_friends_keeps_per_gb_price_for_capped_plans(self):
        plan = settings.get_plan("plan_1m_10gb")
        coupon = settings.get_coupon("FRIENDS")
        self.assertEqual(settings.coupon_price(plan, coupon), 45000)

    def test_fixed_plan_price_never_exceeds_list_price(self):
        plan = dict(settings.get_plan("plan_u_300gb"), price="100000")
        coupon = settings.get_coupon("FRIENDS")
        self.assertEqual(settings.coupon_price(plan, coupon), 100000)

    def test_coupon_with_no_applicable_price_is_none(self):
        plan = settings.get_plan("plan_1m_10gb")
        self.assertIsNone(settings.coupon_price(plan, {"code": "X"}))

    def test_black_takes_ten_percent_off_every_plan(self):
        coupon = settings.get_coupon("BLACK")
        for plan in settings.PLANS:
            expected = int(round(int(plan["price"]) * 0.9))
            self.assertEqual(
                settings.coupon_price(plan, coupon),
                expected,
                plan["id"],
            )

    def test_black_unlimited_price(self):
        plan = settings.get_plan("plan_u_300gb")
        self.assertEqual(
            settings.coupon_price(plan, settings.get_coupon("BLACK")),
            135000,
        )


if __name__ == "__main__":
    unittest.main()
