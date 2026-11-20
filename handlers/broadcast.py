"""Admin broadcast flow: specific user, plan audience, or all users."""
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import settings
from tasks.broadcast import send_broadcast
from utils import safe_edit_text

router = Router()


class BroadcastFlow(StatesGroup):
    waiting_target_user = State()
    waiting_message = State()
    waiting_confirm = State()


def _is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_USER_IDS


def _menu():
    b = InlineKeyboardBuilder()
    b.button(text="👤 یک کاربر مشخص", callback_data="broadcast:target:user")
    b.button(text="📦 کاربران یک پلن فعال", callback_data="broadcast:target:plan")
    b.button(text="👥 همه کاربران", callback_data="broadcast:target:all")
    b.button(text="🔙 بازگشت به پنل", callback_data="admin:home")
    b.adjust(1)
    return b.as_markup()


def _back_menu():
    b = InlineKeyboardBuilder()
    b.button(text="🔙 بازگشت", callback_data="broadcast:home")
    b.adjust(1)
    return b.as_markup()


def _confirm_menu():
    b = InlineKeyboardBuilder()
    b.button(text="✅ ارسال", callback_data="broadcast:confirm")
    b.button(text="❌ لغو", callback_data="broadcast:cancel")
    b.adjust(1)
    return b.as_markup()


def _target_prompt(label: str, count: int) -> str:
    return (
        "📝 <b>پیام موردنظر را ارسال کنید.</b>\n\n"
        f"مخاطب: {escape(label)}\n"
        f"تعداد گیرنده: <b>{count}</b>\n\n"
        "متن می‌تواند شامل HTML مجاز تلگرام باشد."
    )


@router.callback_query(F.data == "broadcast:home")
async def broadcast_home(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await safe_edit_text(
        callback.message,
        "📢 <b>ارسال پیام</b>\n\nمخاطبان را انتخاب کنید:",
        reply_markup=_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "broadcast:target:user")
async def broadcast_target_user(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(BroadcastFlow.waiting_target_user)
    await safe_edit_text(
        callback.message,
        "👤 شناسه عددی تلگرام یا <code>@username</code> کاربر را ارسال کنید:",
        reply_markup=_back_menu(),
    )
    await callback.answer()


@router.message(BroadcastFlow.waiting_target_user, F.text)
async def broadcast_receive_target_user(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    target_id = None
    if raw.startswith("@"):
        target_id = await db.find_telegram_id_by_username(raw)
        if target_id is None:
            await message.answer("❌ کاربری با این username پیدا نشد.")
            return
    else:
        try:
            target_id = int(raw)
        except ValueError:
            await message.answer("❌ شناسه معتبر نیست. عدد یا @username ارسال کنید.")
            return

    label = raw if raw.startswith("@") else f"user {target_id}"
    await state.update_data(
        broadcast_recipients=[target_id],
        broadcast_label=label,
    )
    await state.set_state(BroadcastFlow.waiting_message)
    await message.answer(_target_prompt(label, 1))


@router.message(BroadcastFlow.waiting_target_user)
async def broadcast_target_user_wrong_type(message: Message):
    await message.answer("لطفاً شناسه یا @username را به‌صورت متن ارسال کنید.")


@router.callback_query(F.data == "broadcast:target:plan")
async def broadcast_target_plan(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    counts = await db.count_active_users_by_plan()
    b = InlineKeyboardBuilder()
    plan_count = 0
    for plan in settings.PLANS:
        count = counts.get(plan["id"], 0)
        if count <= 0:
            continue
        plan_count += 1
        b.button(
            text=f"{plan['name']} — {count} کاربر",
            callback_data=f"broadcast:plan:{plan['id']}",
        )
    b.button(text="🔙 بازگشت", callback_data="broadcast:home")
    b.adjust(1)
    if plan_count == 0:
        text = "📦 هیچ پلن فعالی با کاربر فعال وجود ندارد."
    else:
        text = "📦 <b>پلن موردنظر را انتخاب کنید:</b>"
    await safe_edit_text(callback.message, text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast:plan:"))
async def broadcast_select_plan(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    plan_id = callback.data.split(":", 2)[2]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن پیدا نشد.", show_alert=True)
        return
    recipients = await db.get_active_telegram_ids_for_plan(plan_id)
    if not recipients:
        await callback.answer("کاربر فعالی برای این پلن وجود ندارد.", show_alert=True)
        return

    label = f"پلن {plan['name']}"
    await state.update_data(
        broadcast_recipients=recipients,
        broadcast_label=label,
    )
    await state.set_state(BroadcastFlow.waiting_message)
    await safe_edit_text(
        callback.message,
        _target_prompt(label, len(recipients)),
    )
    await callback.answer()


@router.callback_query(F.data == "broadcast:target:all")
async def broadcast_target_all(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    recipients = await db.get_all_telegram_ids()
    if not recipients:
        await callback.answer("هیچ کاربری ثبت نشده است.", show_alert=True)
        return
    label = "همه کاربران"
    await state.update_data(
        broadcast_recipients=recipients,
        broadcast_label=label,
    )
    await state.set_state(BroadcastFlow.waiting_message)
    await safe_edit_text(
        callback.message,
        _target_prompt(label, len(recipients)),
    )
    await callback.answer()


@router.message(BroadcastFlow.waiting_message, F.text)
async def broadcast_receive_message(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if not text:
        await message.answer("پیام خالی است.")
        return
    if len(text) > 4000:
        await message.answer("❌ متن پیام بیش از ۴۰۰۰ کاراکتر است.")
        return

    data = await state.get_data()
    recipients = data.get("broadcast_recipients") or []
    label = data.get("broadcast_label") or "نامشخص"
    if not recipients:
        await state.clear()
        await message.answer("❌ مخاطبان پیدا نشدند. دوباره شروع کنید.")
        return

    await state.update_data(broadcast_text=text)
    await state.set_state(BroadcastFlow.waiting_confirm)
    preview = text if len(text) <= 1200 else text[:1200] + "…"
    await message.answer(
        "📢 <b>تایید ارسال</b>\n\n"
        f"مخاطب: {escape(label)}\n"
        f"تعداد گیرنده: <b>{len(recipients)}</b>\n\n"
        f"<blockquote>{escape(preview)}</blockquote>",
        reply_markup=_confirm_menu(),
    )


@router.message(BroadcastFlow.waiting_message)
async def broadcast_message_wrong_type(message: Message):
    await message.answer("لطفاً متن پیام را ارسال کنید.")


@router.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    recipients = data.get("broadcast_recipients") or []
    text = data.get("broadcast_text")
    if not recipients or not text:
        await callback.answer("اطلاعات ارسال کامل نیست.", show_alert=True)
        return

    try:
        send_broadcast.delay(recipients, text, settings.ADMIN_CHAT_ID)
    except Exception as exc:
        await callback.answer(
            f"ارسال در صف قرار نگرفت: {exc}", show_alert=True
        )
        return

    await state.clear()
    await safe_edit_text(
        callback.message,
        f"✅ پیام برای <b>{len(recipients)}</b> کاربر در صف ارسال قرار گرفت.\n"
        "نتیجه نهایی در همین چت ارسال می‌شود.",
        reply_markup=_back_menu(),
    )
    await callback.answer("در صف ارسال قرار گرفت")


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await safe_edit_text(
        callback.message,
        "ارسال پیام لغو شد.",
        reply_markup=_menu(),
    )
    await callback.answer("لغو شد")
