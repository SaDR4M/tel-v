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
 """
\u200f🔰 آموزش اتصال به Sing-box
\u200fبرای اتصال به کانفیگ و مدیریت سرورها، مراحل زیر رو به ترتیب انجام بدید 👇

\u200f📥 1. دانلود برنامه Sing-box
\u200f📱 اندروید | Google Play
\u200f https://play.google.com/store/apps/details?id=io.nekohasekai.sfa
\u200f🍎 آیفون | App Store
\u200f https://apps.apple.com/us/app/sing-box-mt/id6785326793
\u200f💻 سایر نسخه‌ها | GitHub
\u200f https://github.com/SagerNet/sing-box/releases

\u200f⚙️ 2. افزودن کانفیگ
\u200f➕ روی علامت + کلیک کنید.
\u200f🔹 گزینه Create manually رو انتخاب کنید.
\u200f🔹 یک Name دلخواه برای کانفیگ وارد کنید.
\u200f🔹 گزینه Remote رو انتخاب کنید.
\u200f🔹 در قسمت URL، لینک سابسکریپشن خودتون رو وارد کنید.

\u200f🚀 3. اتصال به سرور
\u200f🔙 یک مرحله به عقب برگردید.
\u200f▶️ روی دکمه ▶️ کلیک کنید تا اتصال برقرار بشه.
\u200f✅ در صورت صحیح بودن اطلاعات، اتصال برقرار خواهد شد.

\u200f📊 4. مشاهده حجم مصرفی و انتخاب سرور
\u200fبعد از اینکه به سرور وصل شدید، برای مشاهده حجم مصرف‌شده، حجم باقی‌مانده و اطلاعات مربوط به سرویس:
\u200f📁 روی آیکون Folder مانند که در کنار دکمه Pause قرار داره بزنید.
\u200f🔹 سپس گزینه Group Service رو انتخاب کنید؛
\u200f🔹 از بین گزینه‌ها، گزینه اول رو انتخاب کنید.
\u200f📊 در این قسمت می‌تونید اطلاعات مربوط به مصرف و وضعیت سرویس رو مشاهده کنید.

\u200f🌐 5. انتخاب سرور برای کاربردهای مختلف
\u200fاز همین بخش Group Service می‌تونید مشخص کنید برای هر نوع استفاده، کدوم سرور فعال باشه.
\u200fمثلاً:
\u200f🌐 Web Browsing → انتخاب سرور مناسب برای وب‌گردی
\u200f🎮 Gaming → انتخاب سرور مناسب برای بازی
\u200f▶️ YouTube → انتخاب سرور مناسب برای یوتیوب
\u200f🔄 بنابراین در صورت نیاز می‌تونید بین سرورها جابه‌جا بشید و برای هر کاربرد، سرور موردنظر خودتون رو انتخاب کنید.

\u200f💡 نکته:
\u200fلینک سابسکریپشن رو دقیق و بدون فاصله وارد کنید تا کانفیگ‌ها به‌درستی دریافت و نمایش داده بشن.
"""
)


class SupportFlow(StatesGroup):
    waiting_for_message = State()


@router.message(F.text == "آموزش استفاده")
async def cb_docs(callback: CallbackQuery):
    # await callback.message.edit_text(DOCS_TEXT, reply_markup=back_home())
    await callback.answer(DOCS_TEXT, reply_markup=back_home())


@router.callback_query(F.data == "menu:docs")
async def cb_menu_docs(callback: CallbackQuery):
    # await callback.message.edit_text(DOCS_TEXT, reply_markup=back_home())
    await callback.message.edit_text(text=DOCS_TEXT, reply_markup=back_home())
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


# @router.callback_query(F.data == "menu:support")
@router.message(F.text == "پشتیبانی")
async def cb_support(callback: CallbackQuery, state: FSMContext):
    text = await _prompt_support(state)
    await callback.answer(text, reply_markup=back_home())


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
