from utils import safe_edit_text

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart

import database as db
from config import settings
from handlers.keyboards import main_menu, main_reply_keyboard

router = Router()

WELCOME_TEXT = (
    f"👋 <b>به {settings.APP_NAME} خوش آمدید</b>\n\n"
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await db.upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(WELCOME_TEXT, reply_markup=main_reply_keyboard)


@router.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery):
    await safe_edit_text(callback.message, WELCOME_TEXT, reply_markup=main_menu())
    await callback.answer()
