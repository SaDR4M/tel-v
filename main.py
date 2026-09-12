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

import psutil
import os
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="باز کردن منوی اصلی"),
    BotCommand(command="plans", description="💳 لیست قیمت / خرید اشتراک"),
    BotCommand(command="mysub", description="📱 مشاهده اشتراک شما"),
    BotCommand(command="docs", description="📖 راهنمای استفاده از اشتراک"),
    BotCommand(command="support", description="🆘 تماس با پشتیبانی"),
]


pid = os.getpid()
process = psutil.Process(pid)

async def server():
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

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def monitor():
    while True:
        try:
            await asyncio.sleep(30)
            mem_info = await asyncio.to_thread(lambda: process.memory_info())
            mem_usage = mem_info.rss / 1024 / 1024
            cpu = await asyncio.to_thread(lambda: process.cpu_percent(interval=30))
            logger.info(f"""Memory usage: {mem_usage:.1f} MB - Cpu usage: {cpu:.4f}%, Time: {time.time()}""")
        except asyncio.CancelledError:
            print("Monitor task cancelled - shutting down gracefully")
            break


async def main():
    # Start monitoring as a separate background task
    monitor_task = asyncio.create_task(monitor())
    
    try:
        # Run the bot server
        await server()
    finally:
        # Cancel the monitor task when server stops
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass  # Expected - we cancelled it

if __name__ == "__main__":
    asyncio.run(main())
