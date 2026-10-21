"""The 'message is not modified' guard used by every callback-driven edit."""
import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_parse_mode_is_forwarded_for_text(self):
        message = SimpleNamespace(edit_text=AsyncMock())
        _run(safe_edit_text(message, "<b>hi</b>", parse_mode="HTML"))
        message.edit_text.assert_awaited_once_with(
            "<b>hi</b>", reply_markup=None, parse_mode="HTML"
        )

    def test_parse_mode_is_forwarded_for_caption(self):
        message = SimpleNamespace(edit_caption=AsyncMock())
        _run(safe_edit_caption(message, "<b>cap</b>", parse_mode="HTML"))
        message.edit_caption.assert_awaited_once_with(
            caption="<b>cap</b>", reply_markup=None, parse_mode="HTML"
        )


class HandlerDoubleTapTest(unittest.TestCase):
    """Reproduces the production crash: a callback tapped twice edits with
    identical content and Telegram answers 'message is not modified'."""

    def _callback(self, data):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data=data,
            message=SimpleNamespace(
                edit_text=AsyncMock(
                    side_effect=_bad_request("Bad Request: message is not modified")
                )
            ),
            answer=AsyncMock(),
        )

    def test_payflow_double_tap(self):
        from handlers import purchase
        state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
        _run(purchase.cb_payflow(self._callback("payflow:plan_1m_10gb"), state))

    def test_menu_and_tier_double_taps(self):
        from handlers import purchase
        _run(purchase.menu_plans(self._callback("menu:plans")))
        _run(purchase.tier_plans(self._callback("tierplans:vip")))

    def test_buy_screen_double_tap(self):
        from handlers import purchase
        state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock())
        _run(purchase.cb_buy(self._callback("buy:plan_1m_10gb"), state))

    def test_order_screen_accepts_html_parse_mode(self):
        from handlers import mysub
        callback = self._callback("order:42")
        callback.message.edit_text = AsyncMock()
        order = {"order_id": 42}
        with patch.object(mysub.db, "get_order", AsyncMock(return_value=order)), \
             patch.object(mysub, "_build_sub_text", AsyncMock(return_value="<b>sub</b>")):
            _run(mysub.order_callback(callback))
        callback.message.edit_text.assert_awaited_once_with(
            "<b>sub</b>", reply_markup=None, parse_mode="HTML"
        )

    def test_real_errors_still_propagate_through_handler(self):
        from handlers import purchase
        c = self._callback("menu:plans")
        c.message.edit_text = AsyncMock(
            side_effect=_bad_request("Bad Request: chat not found")
        )
        with self.assertRaises(TelegramBadRequest):
            _run(purchase.menu_plans(c))


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

    def test_safe_editors_only_receive_supported_kwargs(self):
        """Guards the regression where a callback.edit_text call was swapped
        for safe_edit_text but its old/garbage kwargs (parse_mode=... on the
        wrapper, show_alert=...) were left behind."""
        import ast
        import inspect
        import pathlib
        from utils import safe_edit_caption as caption_fn
        from utils import safe_edit_text as text_fn

        allowed = {
            "safe_edit_text": set(inspect.signature(text_fn).parameters) - {"message"},
            "safe_edit_caption": set(inspect.signature(caption_fn).parameters) - {"message"},
        }
        offenders = []
        root = pathlib.Path(__file__).resolve().parent.parent / "handlers"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Name) or func.id not in allowed:
                    continue
                for keyword in node.keywords:
                    if keyword.arg not in allowed[func.id]:
                        offenders.append(
                            f"{path.name}:{node.lineno}: {func.id}(...{keyword.arg}=...)"
                        )
        self.assertEqual(offenders, [], "unsupported kwargs: " + "; ".join(offenders))

    def test_handlers_annotate_the_object_aiogram_injects(self):
        """A @router.message handler gets a Message, a callback handler gets a
        CallbackQuery. Getting this wrong hides method mismatches (e.g.
        Message.answer has no show_alert) until production."""
        import ast
        import pathlib

        expected = {"message": "Message", "callback_query": "CallbackQuery"}
        offenders = []
        root = pathlib.Path(__file__).resolve().parent.parent / "handlers"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                kinds = []
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    func = decorator.func
                    if (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "router"
                    ):
                        kinds.append(func.attr)
                if not node.args.args:
                    continue
                for kind in kinds:
                    if kind not in expected:
                        continue
                    annotation = node.args.args[0].annotation
                    annotation_name = (
                        annotation.id if isinstance(annotation, ast.Name) else None
                    )
                    if annotation_name != expected[kind]:
                        offenders.append(
                            f"{path.name}:{node.lineno}: {node.name} "
                            f"@{kind} first arg is {annotation_name!r}"
                        )
        self.assertEqual(
            offenders, [], "mistyped handler args: " + "; ".join(offenders)
        )

    def test_show_alert_only_used_on_callback_answers(self):
        """show_alert is a CallbackQuery.answer kwarg; Message.answer rejects it."""
        import ast
        import pathlib

        offenders = []
        root = pathlib.Path(__file__).resolve().parent.parent / "handlers"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr != "answer":
                    continue
                if not any(k.arg == "show_alert" for k in node.keywords):
                    continue
                receiver = func.value.id if isinstance(func.value, ast.Name) else None
                if receiver != "callback":
                    offenders.append(
                        f"{path.name}:{node.lineno}: {receiver}.answer(show_alert=...)"
                    )
        self.assertEqual(
            offenders, [], "show_alert on non-callback: " + "; ".join(offenders)
        )
