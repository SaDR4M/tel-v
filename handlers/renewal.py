from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from config import settings
from panel_api import panel, PanelAPIError
from handlers.mysub import STATUS_LABELS, _fmt_bytes
from handlers.keyboards import (
    renewable_subs_keyboard,
    no_renewable_subs_keyboard,
    renewal_plans_menu,
    confirm_renewal,
    admin_review_keyboard,
    back_home,
)
from utils import safe_edit_text

router = Router()

NEEDS_RENEWAL_STATUSES = {"expired", "disabled", "limited"}
USAGE_THRESHOLD = 0.95


class RenewalFlow(StatesGroup):
    waiting_for_receipt = State()


def _needs_renewal(user: dict) -> bool:
    status = user.get("status", "")
    if status in NEEDS_RENEWAL_STATUSES:
        return True
    data_limit = user.get("data_limit", 0)
    used = user.get("used_traffic", 0)
    if data_limit and data_limit > 0:
        return (used / data_limit) >= USAGE_THRESHOLD
    return False


async def _fetch_renewable_subs(telegram_id: int) -> list[dict]:
    panel_usernames = await db.get_distinct_panel_usernames(telegram_id)
    renewable = []
    for username in panel_usernames:
        try:
            user = await panel.get_user(username)
        except PanelAPIError:
            continue
        if not user:
            continue
        if _needs_renewal(user):
            status_fa = STATUS_LABELS.get(user.get("status", ""), user.get("status", ""))
            used = _fmt_bytes(user.get("used_traffic", 0))
            limit = _fmt_bytes(user.get("data_limit", 0))
            renewable.append(
                {
                    "panel_username": username,
                    "label": f"{username} — {status_fa} — {used}/{limit}",
                }
            )
    return renewable


async def _show_renewal_list(telegram_id: int):
    subs = await _fetch_renewable_subs(telegram_id)
    if not subs:
        text = (
            "شما در حال حاضر اشتراکی که نیاز به تمدید داشته باشد ندارید.\n\n"
            "می‌توانید یک سرویس جدید خریداری کنید:"
        )
        return text, no_renewable_subs_keyboard()

    text = "🔄 <b>تمدید سرویس</b>\n\nاشتراکی که می‌خواهید تمدید کنید را انتخاب کنید:"
    return text, renewable_subs_keyboard(subs)


@router.callback_query(F.data == "menu:renew")
@router.message(F.text == "تمدید سرویس")
async def cb_renew_menu(callback: CallbackQuery):
    text, kb = await _show_renewal_list(callback.from_user.id)
    # await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.answer(text=text, reply_markup=kb)


@router.callback_query(F.data.startswith("renew_select:"))
async def cb_renew_select(callback: CallbackQuery, state: FSMContext):
    panel_username = callback.data.split(":", 1)[1]
    await state.update_data(target_panel_username=panel_username)
    await safe_edit_text(
        callback.message,
        f"📋 <b>تمدید سرویس: {panel_username}</b>\n\nیک پلن برای تمدید انتخاب کنید:",
        reply_markup=renewal_plans_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("renew_plan:"))
async def cb_renew_plan(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 1)[1]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return

    await state.update_data(plan_id=plan_id)

    data_str = "نامحدود" if plan["data_limit_bytes"] == 0 else f"{plan['data_limit_bytes'] / 1_073_741_824:.0f} گیگابایت"
    text = (
        # f"🧾 <b>تمدید با پلن: {plan['name']}</b>\n\n"
        f"قیمت: <b>{plan['price']} {settings.CURRENCY}</b>\n"
        f"حجم: {data_str}\n"
        f"مدت: {plan['duration_days']} روز\n\n"
        f"مبلغ را به این مشخصات واریز کنید: <i>[اطلاعات پرداخت / آدرس کیف پول خود را اینجا قرار دهید]</i>\n\n"
        f"پس از پرداخت، روی دکمه زیر بزنید تا ادامه دهید."
    )
    await safe_edit_text(callback.message, text, reply_markup=confirm_renewal(plan_id))
    await callback.answer()
    

@router.callback_query(F.data.startswith("renewpayflow:"))
async def cb_renewpayflow(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 1)[1]
    await state.update_data(plan_id=plan_id)
    await state.set_state(RenewalFlow.waiting_for_receipt)
    await safe_edit_text(
        callback.message,
        "📎 لطفاً عکس رسید پرداخت را ارسال کنید.",
        reply_markup=back_home(),
    )
    await callback.answer()


@router.message(RenewalFlow.waiting_for_receipt, F.photo)
async def receive_renewal_receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    plan_id = data.get("plan_id")
    target_panel_username = data.get("target_panel_username")
    plan = settings.get_plan(plan_id)
    if not plan or not target_panel_username:
        await message.answer("مشکلی پیش آمد، لطفاً دوباره با /start شروع کنید.")
        await state.clear()
        return

    file_id = message.photo[-1].file_id
    order_id = await db.create_order(
        telegram_id=message.from_user.id,
        plan_id=plan_id,
        requested_username=target_panel_username,
        receipt_file_id=file_id,
        order_type="renewal",
        target_panel_username=target_panel_username,
    )
    await state.clear()

    uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    caption = (
        f"🔄 <b>درخواست تمدید #{order_id}</b>\n"
        f"کاربر: {uname} (شناسه: <code>{message.from_user.id}</code>)\n"
        f"سرویس مورد تمدید: <code>{target_panel_username}</code>\n"
        f"پلن: {plan['name']} — {plan['price']} {settings.CURRENCY}"
    )
    admin_msg = await bot.send_photo(
        chat_id=settings.ADMIN_CHAT_ID,
        photo=file_id,
        caption=caption,
        reply_markup=admin_review_keyboard(order_id),
    )
    await db.set_order_admin_message(order_id, admin_msg.message_id)

    await message.answer(
        "✅ رسید دریافت شد! یک ادمین به‌زودی درخواست تمدید شما را بررسی می‌کند."
    )


@router.message(RenewalFlow.waiting_for_receipt)
async def receive_renewal_receipt_wrong_type(message: Message):
    await message.answer("لطفاً رسید را به‌صورت عکس/اسکرین‌شات ارسال کنید.")