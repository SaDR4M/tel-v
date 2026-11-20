"""Admin broadcast delivery."""
import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter

from app.celery_app import celery_app
from config import settings

logger = logging.getLogger(__name__)


async def _send_all(recipients: list[int], text: str, admin_chat_id: int) -> dict:
    sent = 0
    failed = 0
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    async with bot:
        for chat_id in recipients:
            try:
                try:
                    await bot.send_message(chat_id, text, request_timeout=30)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 1)
                    await bot.send_message(chat_id, text, request_timeout=30)
                sent += 1
            except Exception as exc:
                failed += 1
                logger.warning("Broadcast to %s failed: %s", chat_id, exc)
            await asyncio.sleep(0.05)

        try:
            await bot.send_message(
                admin_chat_id,
                "📢 <b>نتیجه ارسال پیام</b>\n\n"
                f"موفق: <b>{sent}</b>\n"
                f"ناموفق: <b>{failed}</b>",
                request_timeout=30,
            )
        except Exception:
            logger.exception("Could not deliver broadcast summary")

    return {"sent": sent, "failed": failed}


@celery_app.task(soft_time_limit=1800, time_limit=1900)
def send_broadcast(recipients: list[int], text: str, admin_chat_id: int) -> dict:
    return asyncio.run(_send_all(recipients, text, admin_chat_id))
