from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db
from config import settings
from panel_api import panel, PanelAPIError
from utils import dedupe_labels, safe_edit_text
from handlers.keyboards import back_home, main_menu, tiers_menu

router = Router()

MANUAL_PREPARING_TEXT = (
    "⏳ سرویس شما در حال آماده‌سازی است.\n\n"
    "لینک اکانت به‌زودی توسط پشتیبانی برای شما ارسال می‌شود."
)

MANUAL_DELIVERED_TEMPLATE = (
    "📱 <b>اشتراک شما</b>\n\n"
    "لینک اشتراک شما آماده است:\n"
    "<code>{link}</code>\n\n"
    "این لینک را در اپلیکیشن خود وارد کنید. "
    "برای دریافت مجدد، از «🛠 سرویس‌های من» استفاده کنید."
)

STATUS_LABELS = {
    "active": "فعال",
    "disabled": "غیرفعال",
    "limited": "محدود شده (اتمام حجم)",
    "expired": "منقضی شده",
}


def build_orders_keyboard(orders, names: dict[str, str] | None = None) -> InlineKeyboardMarkup:
    """One button per subscription. Users see the nickname they picked when
    ordering plus the plan size — the internal panel username stays hidden
    in callback_data and only appears as a fallback for legacy accounts."""
    names = names or {}
    entries = []
    for order in orders:
        entries.append((order, _order_label(order, names)))
    # Two orders can share a nickname (e.g. "Sadra" bought twice) -> identical
    # buttons; tag consecutive duplicates so the lists stay unambiguous.
    labels = dedupe_labels([entry[1] for entry in entries])
    buttons = []
    for (order, _), label in zip(entries, labels):
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"order:{order.get('order_id')}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _order_label(order: dict, names: dict[str, str]) -> str:
    size = ""
    plan = settings.get_plan(order.get("plan_id") or "")
    if plan:
        data_limit = int(plan.get("data_limit_bytes") or 0)
        size = "نامحدود" if data_limit == 0 else f"{data_limit / 1_073_741_824:,.0f} گیگ"
        size = f" — {size}"
    nickname = names.get(order.get("panel_username"))
    if not nickname:
        req = order.get("requested_username")
        # A requested name identical to the panel username (legacy/renewal
        # rows) is not a nickname, so don't parade the internal name around.
        if req and req != order.get("panel_username"):
            nickname = req
    if nickname:
        return f"«{nickname}»{size}"
    panel_username = order.get("panel_username")
    if panel_username:
        return f"اشتراک {panel_username}{size}"
    return f"اشتراک{size}"


def _fmt_bytes(n: int) -> str:
    if n == 0:
        return 0
    gb = n / 1_073_741_824
    return f"{gb:.2f} گیگابایت"


async def _build_sub_text(order_id: int) -> str:
    order = await db.get_order(order_id)
    if not order:
        return "سفارش پیدا نشد."

    panel_username = order.get("panel_username")
    if not panel_username:
        # Manual-tier order (no bot panel account): the admin delivers the
        # link by replying to the order in the admin group.
        manual_link = (order.get("manual_link") or "").strip()
        if manual_link:
            return MANUAL_DELIVERED_TEMPLATE.format(link=manual_link)
        return MANUAL_PREPARING_TEXT

    try:
        user = await panel.get_user(panel_username)
    except PanelAPIError as e:
        return f"⚠️ اتصال به پنل ناموفق بود: {e}"

    if not user:
        return "اشتراکی برای حساب شما یافت نشد."

    expire_str = (
        datetime.fromtimestamp(user["expire"]).strftime("%Y-%m-%d")
        if user.get("expire")
        else "نامشخص"
    )
    status_raw = user.get("status", "unknown")
    status_fa = STATUS_LABELS.get(status_raw, status_raw)
    used = _fmt_bytes(user.get("used_traffic", 0))
    limit = _fmt_bytes(user.get("data_limit", 0))

    return (
        f"📱 <b>اشتراک شما</b>\n\n"
        f"وضعیت: <b>{status_fa}</b>\n"
        f"مصرف‌شده: {used} از {limit}\n"
        f"تاریخ انقضا: {expire_str}\n\n"
        f"لینک اشتراک:\n<code>{user.get('subscription_url', 'نامشخص')}</code>"
    )


@router.message(Command("mysub"))
async def mysub_handler(message: Message):
    orders = await db.get_active_orders(
        telegram_id=message.from_user.id
    )
    if not orders:
        return await message.answer(
            "اشتراکی برای شما یافت نشد"
        )
    names = await db.get_display_names(message.from_user.id)
    await message.answer(
        "اشتراک های شما:\n",
        reply_markup=build_orders_keyboard(orders, names),
    )

@router.callback_query(F.data.startswith("order:"))
async def order_callback(callback: CallbackQuery):

    print(callback.data)
    order_id = int(callback.data.split(":")[1])

    order = await db.get_order(
        order_id=order_id,
    )

    if not order:
        await callback.answer(
            "سفارش پیدا نشد.",
            show_alert=True,
        )
        return

    text = await _build_sub_text(
        order_id=order["order_id"]
    )

    await safe_edit_text(callback.message, 
        text,
        parse_mode="HTML",
    )

    await callback.answer()


@router.message(F.text == "سرویس‌های من")
async def mysub_callback(callback: CallbackQuery):
    orders = await db.get_active_orders(
        telegram_id=callback.from_user.id
    )

    if not orders:
        await callback.answer(
            """
            شما هنوز اشتراک فعالی ندارید
            """,
            reply_markup=tiers_menu()
        )
        return

    names = await db.get_display_names(callback.from_user.id)
    await callback.answer(
        "📦 اشتراک‌های فعال شما:",
        reply_markup=build_orders_keyboard(orders, names),
    )


@router.callback_query(F.data == "menu:mysub")
async def mysub_menu_callback(callback: CallbackQuery):
    orders = await db.get_active_orders(
        telegram_id=callback.from_user.id
    )

    if not orders:
        await safe_edit_text(callback.message, 
            text="""
            شما هنوز اشتراک فعالی ندارید
            """,
            reply_markup=tiers_menu()
        )
        await callback.answer()
        return

    names = await db.get_display_names(callback.from_user.id)
    await safe_edit_text(callback.message, 
        text="📦 اشتراک‌های فعال شما:",
        reply_markup=build_orders_keyboard(orders, names),
    )
    await callback.answer()

# @router.message(Command("mysub"))
# async def cmd_mysub(message: Message):
#     text = await _build_mysub_text(message.from_user.id)
#     await message.answer(text, reply_markup=back_home())

