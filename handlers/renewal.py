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
from utils import dedupe_labels, safe_edit_text, payment_card_line
from datetime import datetime, timedelta

router = Router()

NEEDS_RENEWAL_STATUSES = {"expired", "disabled", "limited"}
USAGE_THRESHOLD = 0.90


class RenewalFlow(StatesGroup):
    waiting_for_receipt = State()
    waiting_for_coupon = State()


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
    names = await db.get_display_names(telegram_id)
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
            nickname = names.get(username)
            who = f"«{nickname}»" if nickname else username
            renewable.append(
                {
                    "panel_username": username,
                    "label": f"{who} — {status_fa} — {used}/{limit}",
                }
            )
    # Duplicate nicknames (same buyer, two accounts) get #1/#2 tags so the
    # selection list stays unambiguous; ordering is most-recent-first.
    labels = dedupe_labels([entry["label"] for entry in renewable])
    for entry, label in zip(renewable, labels):
        entry["label"] = label
    return renewable


async def _show_renewal_list(telegram_id: int):
    subs = await _fetch_renewable_subs(telegram_id)
    if not subs:
        text = (
            "شما در حال حاضر اشتراکی که نیاز به تمدید داشته باشد ندارید.\n\n"
            "می‌توانید یک سرویس جدید خریداری کنید:"
        )
        # Accounts with no bot-side panel username (eco/unlimited tiers) can't
        # appear above — if the buyer still owns live orders, point their
        # renewal path out explicitly instead of a generic dead end.
        orders = await db.get_active_orders(telegram_id=telegram_id)
        if orders:
            text += (
                "\n\n💡 سرویس‌های دسته‌ی اکونومی و نامحدود با خرید مجدد همان "
                "اشتراک تمدید می‌شوند — سفارش جدید بدهید تا سرویس شما ادامه پیدا کند."
            )
        return text, no_renewable_subs_keyboard()

    text = "🔄 <b>تمدید سرویس</b>\n\nاشتراکی که می‌خواهید تمدید کنید را انتخاب کنید:"
    return text, renewable_subs_keyboard(subs)


@router.message(F.text == "تمدید سرویس")
async def cb_renew_menu(callback: CallbackQuery):
    text, kb = await _show_renewal_list(callback.from_user.id)
    # await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.answer(text=text, reply_markup=kb)


@router.callback_query(F.data == "menu:renew")
async def cb_menu_renew_menu(callback: CallbackQuery):
    text, kb = await _show_renewal_list(callback.from_user.id)
    # await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.message.edit_text(text=text, reply_markup=kb)
    await callback.answer()


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

    await state.update_data(plan_id=plan_id, coupon_code=None)

    await safe_edit_text(callback.message, _renew_offer_text(plan), reply_markup=confirm_renewal(plan_id))
    await callback.answer()


def _renew_offer_text(plan: dict, coupon_code: str | None = None) -> str:
    data_str = "نامحدود" if plan["data_limit_bytes"] == 0 else f"{plan['data_limit_bytes'] / 1_073_741_824:.0f} گیگابایت"
    price_line = f"قیمت: <b>{plan['price']} {settings.CURRENCY}</b>"
    if coupon_code:
        coupon = settings.get_coupon(coupon_code)
        final = settings.coupon_price(plan, coupon) if coupon else None
        if final is not None and final < int(plan["price"]):
            price_line = (
                f"قیمت: <b>{final} {settings.CURRENCY}</b>\n"
                f"🎟 تخفیف «{coupon_code}» اعمال شد"
            )
    return (
        # f"🧾 <b>تمدید با پلن: {plan['name']}</b>\n\n"
        f"{price_line}\n"
        f"حجم: {data_str}\n"
        f"مدت: {plan['duration_days']} روز\n\n"
        "مبلغ را به این مشخصات واریز کنید:\n\n"
        f"{payment_card_line()}\n\n"
        "پس از پرداخت، روی دکمه زیر بزنید تا ادامه دهید."
    )


@router.callback_query(F.data.startswith("rcoupon:ask:"))
async def cb_renewal_coupon_ask(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 2)[2]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return

    await state.update_data(plan_id=plan_id)
    await state.set_state(RenewalFlow.waiting_for_coupon)
    await safe_edit_text(
        callback.message,
        "🎟 کد تخفیف را ارسال کنید:",
        reply_markup=back_home(),
    )
    await callback.answer()


@router.message(RenewalFlow.waiting_for_coupon, F.text)
async def receive_renewal_coupon(message: Message, state: FSMContext):
    data = await state.get_data()
    plan_id = data.get("plan_id")
    plan = settings.get_plan(plan_id)
    if not plan:
        await state.clear()
        await message.answer("مشکلی پیش آمد، لطفاً دوباره با /start شروع کنید.")
        return

    coupon = settings.get_coupon(message.text)
    if not coupon:
        await message.answer("❌ کد تخفیف معتبر نیست. دوباره تلاش کنید یا بازگشت را بزنید.")
        return
    final = settings.coupon_price(plan, coupon)
    if final is None or final >= int(plan["price"]):
        await message.answer("این کد برای این پلن تخفیفی ندارد.")
        return

    coupon_code = str(coupon.get("code", "")).strip().upper()
    await state.update_data(coupon_code=coupon_code)
    await state.set_state(None)
    await message.answer(
        _renew_offer_text(plan, coupon_code),
        reply_markup=confirm_renewal(plan_id, coupon_code),
    )


@router.message(RenewalFlow.waiting_for_coupon)
async def receive_renewal_coupon_wrong_type(message: Message):
    await message.answer("لطفاً کد تخفیف را به‌صورت متن ارسال کنید.")


@router.callback_query(F.data.startswith("rcoupon:remove:"))
async def cb_renewal_coupon_remove(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 2)[2]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return

    await state.update_data(plan_id=plan_id, coupon_code=None)
    await state.set_state(None)
    await safe_edit_text(
        callback.message, _renew_offer_text(plan), reply_markup=confirm_renewal(plan_id)
    )
    await callback.answer("تخفیف حذف شد.")
    

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
    expire_at = datetime.now() + timedelta(days=29)
    if not plan or not target_panel_username:
        await message.answer("مشکلی پیش آمد، لطفاً دوباره با /start شروع کنید.")
        await state.clear()
        return

    # Re-resolve the coupon at receipt time (it may have been removed since).
    coupon_code = data.get("coupon_code")
    coupon = settings.get_coupon(coupon_code) if coupon_code else None
    final_price = settings.coupon_price(plan, coupon) if coupon else None
    if final_price is None:
        coupon_code = None

    file_id = message.photo[-1].file_id
    order_id = await db.create_order(
        telegram_id=message.from_user.id,
        plan_id=plan_id,
        requested_username=target_panel_username,
        receipt_file_id=file_id,
        expire_at=expire_at,
        order_type="renewal",
        target_panel_username=target_panel_username,
        coupon_code=coupon_code,
        final_price=final_price,
    )
    await state.clear()

    uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    coupon_line = (
        f"\n🎟 تخفیف: <code>{coupon_code}</code> — مبلغ پرداختی: <b>{final_price} {settings.CURRENCY}</b>"
        if final_price is not None else ""
    )
    caption = (
        f"🔄 <b>درخواست تمدید #{order_id}</b>\n"
        f"کاربر: {uname} (شناسه: <code>{message.from_user.id}</code>)\n"
        f"سرویس مورد تمدید: <code>{target_panel_username}</code>\n"
        f"پلن: {plan['name']} — {plan['price']} {settings.CURRENCY}"
        f"{coupon_line}"
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
