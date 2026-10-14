import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(chat_id=1, user_id=1, bot_id=1),
    )


def _message(**kwargs) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=1, type="private"),
        **kwargs,
    )


def _callback(data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=1, is_bot=False, first_name="t"),
        chat_instance="x",
        data=data,
    )


class CancelStaleStateMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, event, with_state="flow:step"):
        from middlewares import CancelStaleStateMiddleware

        state = _state()
        await state.set_state(with_state)
        handler = AsyncMock()
        await CancelStaleStateMiddleware()(handler, event, {"state": state})
        handler.assert_awaited_once()
        return await state.get_state()

    async def test_command_clears_state(self):
        self.assertIsNone(await self._run(_message(text="/mysub")))

    async def test_menu_button_clears_state(self):
        self.assertIsNone(await self._run(_message(text="پشتیبانی")))

    async def test_back_home_callback_clears_state(self):
        self.assertIsNone(await self._run(_callback("menu:home")))

    async def test_menu_navigation_callback_clears_state(self):
        self.assertIsNone(await self._run(_callback("menu:mysub")))

    async def test_plain_text_keeps_state(self):
        self.assertEqual(
            await self._run(_message(text="myname123")), "flow:step"
        )

    async def test_photo_keeps_state(self):
        self.assertEqual(await self._run(_message()), "flow:step")

    async def test_in_flow_callback_keeps_state(self):
        self.assertEqual(await self._run(_callback("buy:plan_x")), "flow:step")
        self.assertEqual(
            await self._run(_callback("renewpayflow:plan_x")), "flow:step"
        )

    async def test_missing_state_does_not_break(self):
        from middlewares import CancelStaleStateMiddleware

        handler = AsyncMock()
        await CancelStaleStateMiddleware()(
            handler, _message(text="/start"), {}
        )
        handler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
