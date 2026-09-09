import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

class SupportReplyHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_reply_is_copied_to_the_original_user(self):
        from handlers import docs_support

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            chat=SimpleNamespace(id=20),
            reply_to_message=SimpleNamespace(message_id=30),
            message_id=40,
            reply=AsyncMock(),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            copy_message=AsyncMock(),
        )

        with (
            patch.object(docs_support.settings, "ADMIN_CHAT_ID", 20),
            patch.object(docs_support.settings, "ADMIN_USER_IDS", {10}),
            patch.object(
                docs_support.db,
                "get_support_message_user",
                AsyncMock(return_value=50),
            ),
        ):
            await docs_support.forward_admin_support_reply(message, bot)

        bot.copy_message.assert_awaited_once_with(
            chat_id=50,
            from_chat_id=20,
            message_id=40,
        )
        bot.send_message.assert_awaited_once_with(
            chat_id=50,
            text="👤 <b>پیام از پشتیبانی</b>",
        )
        message.reply.assert_awaited_once_with("✅ پاسخ برای کاربر ارسال شد.")
