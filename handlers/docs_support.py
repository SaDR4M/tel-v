import logging

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import settings
import database as db
from handlers.keyboards import back_home

router = Router()
logger = logging.getLogger(__name__)

DOCS_TEXT = (
    "📖 <b>نحوه استفاده از اشتراک شما</b>\n\n"
    "۱. لینک اشتراک خود را از بخش «📱 اشتراک من» کپی کنید.\n\n"
    "<b>اندروید:</b> برنامه v2rayNG را نصب کنید ← روی «+» بزنید ← گزینه «Import from clipboard» را انتخاب کنید.\n"
    "<b>iOS:</b> برنامه Streisand یا Shadowrocket را نصب کنید ← اشتراک را از طریق لینک اضافه کنید.\n"
    "<b>ویندوز:</b> برنامه v2rayN را نصب کنید ← بخش Subscriptions ← لینک را اضافه کنید ← Update بزنید.\n"
    "<b>مک:</b> برنامه V2Box یا ClashX را نصب کنید ← لینک اشتراک را اضافه کنید.\n\n"
    "پس از اضافه‌کردن لینک، یک سرور را انتخاب و متصل شوید."
)


class SupportFlow(StatesGroup):
    waiting_for_message = State()


@router.callback_query(F.data == "menu:docs")
async def cb_docs(callback: CallbackQuery):
    await callback.message.edit_text(DOCS_TEXT, reply_markup=back_home())
    await callback.answer()


@router.message(Command("docs"))
async def cmd_docs(message: Message):
    await message.answer(DOCS_TEXT, reply_markup=back_home())


async def _prompt_support(state: FSMContext) -> str:
    await state.set_state(SupportFlow.waiting_for_message)
    return (
        f"🆘 <b>پشتیبانی {settings.APP_NAME}</b>\n\nسوال خود را در پیام بعدی بنویسید تا برای تیم ما ارسال شود "
        f"(یا مستقیماً با {settings.SUPPORT_CONTACT} تماس بگیرید)."
    )


@router.callback_query(F.data == "menu:support")
async def cb_support(callback: CallbackQuery, state: FSMContext):
    text = await _prompt_support(state)
    await callback.message.edit_text(text, reply_markup=back_home())
    await callback.answer()


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    text = await _prompt_support(state)
    await message.answer(text, reply_markup=back_home())


@router.message(SupportFlow.waiting_for_message)
async def receive_support_message(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    admin_message = await bot.send_message(
        chat_id=settings.ADMIN_CHAT_ID,
        text=f"🆘 <b>درخواست پشتیبانی</b> از {uname} (شناسه: <code>{message.from_user.id}</code>):\n\n{message.text}",
    )
    await db.create_support_message(admin_message.message_id, message.from_user.id)
    await message.answer("✅ پیام شما برای پشتیبانی ارسال شد. به‌زودی پاسخ داده خواهد شد.")


@router.message(F.chat.id == settings.ADMIN_CHAT_ID, F.reply_to_message)
async def forward_admin_support_reply(message: Message, bot: Bot):
    """Copies an admin reply to the user whose support message was quoted."""
    if not message.from_user or message.from_user.id not in settings.ADMIN_USER_IDS:
        return

    user_telegram_id = await db.get_support_message_user(message.reply_to_message.message_id)
    if user_telegram_id is None:
        return

    try:
        await bot.send_message(
            chat_id=user_telegram_id,
            text="👤 <b>پیام از پشتیبانی</b>",
        )
        await bot.copy_message(
            chat_id=user_telegram_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning(
            "Could not deliver support reply to user %s: %s",
            user_telegram_id,
            exc,
        )
        await message.reply("❌ ارسال پاسخ به کاربر ناموفق بود. ممکن است کاربر بات را مسدود کرده باشد.")
        return

    await message.reply("✅ پاسخ برای کاربر ارسال شد.")
