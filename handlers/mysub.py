from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db
from panel_api import panel, PanelAPIError
from handlers.keyboards import back_home, main_menu, plans_menu

router = Router()

STATUS_LABELS = {
    "active": "فعال",
    "disabled": "غیرفعال",
    "limited": "محدود شده (اتمام حجم)",
    "expired": "منقضی شده",
}


def _fmt_bytes(n: int) -> str:
    if n == 0:
        return 0
    gb = n / 1_073_741_824
    return f"{gb:.2f} گیگابایت"


async def _build_sub_text(order_id: int) -> str:
    panel_username = await db.get_panel_username(order_id)
    print(order_id)
    print(panel_username)
    if not panel_username:
        return "شما هنوز اشتراک فعالی ندارید. از «💳 لیست قیمت / خرید اشتراک» برای تهیه اشتراک استفاده کنید."

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


def build_orders_keyboard(orders):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"اشتراک {order.get("panel_username")}",
                    callback_data=f"order:{order.get("order_id")}",
                )
            ]
            for order in orders
        ]
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
    await message.answer(
        "اشتراک های شما:\n",
        reply_markup=build_orders_keyboard(orders),
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

    await callback.message.edit_text(
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
            reply_markup=plans_menu()
        )
        return

    await callback.answer(
        "📦 اشتراک‌های فعال شما:",
        reply_markup=build_orders_keyboard(orders),
    )


@router.callback_query(F.data == "menu:mysub")
async def mysub_menu_callback(callback: CallbackQuery):
    orders = await db.get_active_orders(
        telegram_id=callback.from_user.id
    )

    if not orders:
        await callback.message.edit_text(
            text="""
            شما هنوز اشتراک فعالی ندارید
            """,
            reply_markup=plans_menu()
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        text="📦 اشتراک‌های فعال شما:",
        reply_markup=build_orders_keyboard(orders),
    )
    await callback.answer()

# @router.message(Command("mysub"))
# async def cmd_mysub(message: Message):
#     text = await _build_mysub_text(message.from_user.id)
#     await message.answer(text, reply_markup=back_home())

