from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from handlers.keyboards import MENU_BUTTONS


class CancelStaleStateMiddleware(BaseMiddleware):
    """Drops an abandoned order/support flow when the user navigates away.

    FSM states linger until a matching handler consumes them, so without this
    a user who leaves a flow mid-step (runs a /command, taps a menu button,
    hits back-home) has their next update swallowed by the stale step's
    handler — e.g. a command saved as a panel username, or a stray photo
    turned into an order. Clearing here lets normal routing handle the new
    intent; entering a new flow sets its own state afterwards, so in-flow
    callbacks (buy:, payflow:, renew_*, ...) are deliberately left alone.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if isinstance(state, FSMContext) and self._navigated_away(event):
            await state.clear()
        return await handler(event, data)

    @staticmethod
    def _navigated_away(event: TelegramObject) -> bool:
        if isinstance(event, Message):
            text = event.text or ""
            return text.startswith("/") or text in MENU_BUTTONS
        if isinstance(event, CallbackQuery):
            return bool(event.data and event.data.startswith("menu:"))
        return False
