from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import settings
from utils import safe_edit_text
from handlers.test_plan import TEST_PLAN_ID

router = Router()

GB = 1024 ** 3
_MSG_LIMIT = 4000


def _is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_USER_IDS


def _admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="💰 گزارش فروش کل", callback_data="admin:total")
    b.button(text="👥 فروش به تفکیک خریدار", callback_data="admin:buyers")
    b.adjust(1)
    return b.as_markup()


def _report_menu():
    b = InlineKeyboardBuilder()
    b.button(text="🔙 بازگشت به پنل", callback_data="admin:home")
    return b.as_markup()


def _sold_lines(rows):
    """Yield (telegram_id, username, price, bytes) for priced, non-test orders.

    Free test plans and unknown plan ids carry no price, so they are left out
    of the money/volume totals.
    """
    for r in rows:
        if r["plan_id"] == TEST_PLAN_ID:
            continue
        plan = settings.get_plan(r["plan_id"])
        if not plan:
            continue
        # A coupon order counts at the amount actually paid.
        price = r.get("final_price")
        if price is None:
            price = int(plan["price"])
        yield (
            r["telegram_id"],
            r.get("telegram_username"),
            int(price),
            int(plan.get("data_limit_bytes") or 0),
        )


def _total_report(rows) -> str:
    lines = list(_sold_lines(rows))
    if not lines:
        return "💰 هنوز فروش ثبت‌شده‌ای وجود ندارد."
    total_price = sum(price for _, _, price, _ in lines)
    total_bytes = sum(size for _, _, _, size in lines)
    unlimited = sum(1 for _, _, _, size in lines if size == 0)
    text = (
        "💰 <b>گزارش فروش کل</b>\n\n"
        f"تعداد سفارش: <b>{len(lines)}</b>\n"
        f"حجم فروخته‌شده: <b>{total_bytes / GB:,.1f} گیگابایت</b>\n"
        f"مبلغ کل: <b>{total_price:,} {settings.CURRENCY}</b>"
    )
    if unlimited:
        text += f"\n<i>شامل {unlimited} سفارش نامحدود (در مبلغ حساب شده، بدون حجم)</i>"
    return text


def _buyers_chunks(rows) -> list[str]:
    agg: dict[int, dict] = {}
    for telegram_id, username, price, size in _sold_lines(rows):
        entry = agg.setdefault(
            telegram_id,
            {"username": None, "orders": 0, "price": 0, "bytes": 0},
        )
        if username and not entry["username"]:
            entry["username"] = username
        entry["orders"] += 1
        entry["price"] += price
        entry["bytes"] += size
    if not agg:
        return ["👥 هنوز فروش ثبت‌شده‌ای وجود ندارد."]

    header = "👥 <b>فروش به تفکیک خریدار</b>"
    chunks, current = [], header
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["price"], reverse=True)
    for telegram_id, e in ranked:
        who = f"@{e['username']}" if e["username"] else f"<code>{telegram_id}</code>"
        line = (
            f"{who} — {e['orders']} سفارش — "
            f"{e['bytes'] / GB:,.1f} گیگ — {e['price']:,} {settings.CURRENCY}"
        )
        if len(current) + 1 + len(line) > _MSG_LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


@router.message(Command("admin"))
async def cb_admin(message: Message):
    if not _is_admin(message.from_user.id):
        return  # silent: the panel doesn't exist for non-admins
    await message.answer(
        "🛠 <b>پنل مدیریت</b>\nگزارش موردنظر را انتخاب کنید:",
        reply_markup=_admin_menu(),
    )


@router.callback_query(F.data == "admin:home")
async def cb_admin_home(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await safe_edit_text(
        callback.message,
        "🛠 <b>پنل مدیریت</b>\nگزارش موردنظر را انتخاب کنید:",
        reply_markup=_admin_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:total")
async def cb_admin_total(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows = await db.get_sales_data()
    await safe_edit_text(
        callback.message, _total_report(rows), reply_markup=_report_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "admin:buyers")
async def cb_admin_buyers(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows = await db.get_sales_data()
    chunks = _buyers_chunks(rows)
    await safe_edit_text(
        callback.message, chunks[0], reply_markup=_report_menu()
    )
    for chunk in chunks[1:]:
        await callback.message.answer(chunk)
    await callback.answer()
