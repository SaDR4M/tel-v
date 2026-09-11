import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import settings
import database as db
from handlers import menu, purchase, admin_review, mysub, docs_support, test_plan, renewal

logging.basicConfig(level=logging.INFO)


BOT_COMMANDS = [
    BotCommand(command="start", description="باز کردن منوی اصلی"),
    BotCommand(command="plans", description="💳 لیست قیمت / خرید اشتراک"),
    BotCommand(command="mysub", description="📱 مشاهده اشتراک شما"),
    BotCommand(command="docs", description="📖 راهنمای استفاده از اشتراک"),
    BotCommand(command="support", description="🆘 تماس با پشتیبانی"),
]


async def main():
    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    await db.init_db()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(menu.router)
    dp.include_router(purchase.router)
    dp.include_router(admin_review.router)
    dp.include_router(mysub.router)
    dp.include_router(docs_support.router)
    dp.include_router(test_plan.router)
    dp.include_router(renewal.router)

    await bot.set_my_commands(BOT_COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
