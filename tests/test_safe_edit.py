"""The 'message is not modified' guard used by every callback-driven edit."""
import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, ".")

from aiogram.exceptions import TelegramBadRequest
from utils import safe_edit_caption, safe_edit_text


def _run(coro):
    return asyncio.run(coro)


def _bad_request(description: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=description)


class SafeEditTest(unittest.TestCase):
    def test_not_modified_is_swallowed_for_text(self):
        message = SimpleNamespace(
            edit_text=AsyncMock(
                side_effect=_bad_request("Bad Request: message is not modified")
            )
        )
        _run(safe_edit_text(message, "hi", reply_markup=None))  # must not raise
        message.edit_text.assert_awaited_once()

    def test_other_telegram_errors_still_raise_for_text(self):
        message = SimpleNamespace(
            edit_text=AsyncMock(
                side_effect=_bad_request("Bad Request: message to edit not found")
            )
        )
        with self.assertRaises(TelegramBadRequest):
            _run(safe_edit_text(message, "hi"))

    def test_not_modified_is_swallowed_for_caption(self):
        message = SimpleNamespace(
            edit_caption=AsyncMock(
                side_effect=_bad_request("Bad Request: message is not modified")
            )
        )
        _run(safe_edit_caption(message, "cap"))
        message.edit_caption.assert_awaited_once_with(caption="cap", reply_markup=None)

    def test_other_telegram_errors_still_raise_for_caption(self):
        message = SimpleNamespace(
            edit_caption=AsyncMock(
                side_effect=_bad_request("Bad Request: not enough rights")
            )
        )
        with self.assertRaises(TelegramBadRequest):
            _run(safe_edit_caption(message, "cap"))


class EditSitesTest(unittest.TestCase):
    """Every callback edit in the handlers goes through the safe wrappers."""

    def test_no_direct_edits_left_in_handlers(self):
        import pathlib
        offenders = []
        root = pathlib.Path(__file__).resolve().parent.parent / "handlers"
        for path in root.glob("*.py"):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if (".edit_text(" in line or ".edit_caption(" in line) \
                        and "safe_edit" not in line and "def " not in line \
                        and not line.strip().startswith("#"):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "unprotected edit calls: " + "; ".join(offenders))

    def test_all_safe_editors_are_importable(self):
        import handlers.menu, handlers.purchase, handlers.test_plan
        import handlers.docs_support, handlers.mysub, handlers.renewal
        import handlers.admin_review, handlers.admin  # noqa: F401
