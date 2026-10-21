"""Which subscriptions the «تمدید سرویس» menu offers for renewal."""
import sys
import time
import unittest

sys.path.insert(0, ".")

from handlers import renewal


GB = 1024 ** 3


def _user(**overrides):
    user = {
        "status": "active",
        "data_limit": 100 * GB,
        "used_traffic": 0,
        "expire": int(time.time()) + 30 * 86400,
    }
    user.update(overrides)
    return user


class RenewalWindowTest(unittest.TestCase):
    def test_active_and_far_from_limits_is_not_renewable(self):
        self.assertFalse(renewal._needs_renewal(_user()))

    def test_active_but_near_expiry_is_renewable(self):
        self.assertTrue(
            renewal._needs_renewal(
                _user(expire=int(time.time()) + 86400)
            )
        )

    def test_active_but_past_expiry_is_renewable(self):
        self.assertTrue(
            renewal._needs_renewal(
                _user(expire=int(time.time()) - 60)
            )
        )

    def test_near_usage_limit_is_renewable(self):
        self.assertTrue(
            renewal._needs_renewal(_user(used_traffic=int(95 * GB)))
        )

    def test_dead_panel_status_is_renewable_regardless_of_limits(self):
        for status in renewal.NEEDS_RENEWAL_STATUSES:
            self.assertTrue(renewal._needs_renewal(_user(status=status)))

    def test_unlimited_account_without_expiry_is_not_renewable(self):
        self.assertFalse(
            renewal._needs_renewal(_user(data_limit=0, expire=0))
        )

    def test_malformed_expiry_does_not_break_the_menu(self):
        self.assertFalse(renewal._needs_renewal(_user(expire="not-a-number")))


if __name__ == "__main__":
    unittest.main()
