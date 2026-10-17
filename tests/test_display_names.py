import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from utils import dedupe_labels
from handlers.admin_review import _free_panel_username, PanelAPIError
from handlers import mysub
from tasks import subs as tasks_subs


class DedupeLabelsTest(unittest.TestCase):
    def test_duplicates_get_numbered_in_list_order(self):
        out = dedupe_labels(["«Sadra» — 10 گیگ", "«Ali» — 15 گیگ", "«Sadra» — 10 گیگ"])
        self.assertEqual(out, ["«Sadra» — 10 گیگ #1", "«Ali» — 15 گیگ", "«Sadra» — 10 گیگ #2"])

    def test_unique_labels_untouched(self):
        labels = ["«Sadra» — 10 گیگ", "«Sadra» — 15 گیگ"]
        self.assertEqual(dedupe_labels(list(labels)), labels)


class UniquePanelUsernameTest(unittest.IsolatedAsyncioTestCase):
    async def test_free_name_used_as_is(self):
        with patch("handlers.admin_review.panel.get_user", new=AsyncMock(return_value=None)):
            self.assertEqual(await _free_panel_username("office_123"), "office_123")

    async def test_collisions_pick_first_free_variant(self):
        taken = {"office_123": {"username": "office_123"},
                 "office_123_2": {"username": "office_123_2"}}
        async def get_user(name):
            return taken.get(name)
        with patch("handlers.admin_review.panel.get_user", new=get_user):
            self.assertEqual(await _free_panel_username("office_123"), "office_123_3")

    async def test_exhausted_variants_raise_panel_error(self):
        async def get_user(name):
            return {"username": name}
        with patch("handlers.admin_review.panel.get_user", new=get_user):
            with self.assertRaises(PanelAPIError):
                await _free_panel_username("office_123")


class OrderLabelTest(unittest.TestCase):
    def test_nickname_with_plan_size(self):
        label = mysub._order_label(
            {"order_id": 1, "panel_username": "office_123_2",
             "requested_username": "office", "plan_id": "plan_1m_100gb"},
            {"office_123_2": "office"},
        )
        self.assertEqual(label, "«office» — 100 گیگ")

    def test_fallback_to_raw_panel_username(self):
        label = mysub._order_label(
            {"order_id": 2, "panel_username": "legacy_x",
             "requested_username": None, "plan_id": "plan_1m_10gb"},
            {},
        )
        self.assertEqual(label, "اشتراک legacy_x — 10 گیگ")

    def test_no_nickname_when_requested_equals_panel_username(self):
        label = mysub._order_label(
            {"order_id": 3, "panel_username": "u", "requested_username": "u",
             "plan_id": "plan_1m_10gb"},
            {},
        )
        self.assertTrue(label.startswith("اشتراک"), label)

    def test_unknown_plan_id_is_tolerated(self):
        label = mysub._order_label(
            {"order_id": 4, "panel_username": "x", "requested_username": "doc", "plan_id": "ghost"},
            {"x": "doc"},
        )
        self.assertEqual(label, "«doc»")


class ReminderTitleTest(unittest.TestCase):
    def test_uses_plan_name(self):
        order = SimpleNamespace(plan_id="plan_1m_100gb", panel_username="office_123")
        self.assertEqual(tasks_subs._sub_title(order), "100 گیگابایت / 30 روز")

    def test_falls_back_to_panel_username(self):
        order = SimpleNamespace(plan_id="ghost", panel_username="office_123")
        self.assertEqual(tasks_subs._sub_title(order), "office_123")


if __name__ == "__main__":
    unittest.main()
