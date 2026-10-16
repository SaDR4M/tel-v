from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from datetime import datetime, timedelta

import database as db
from config import settings
from models import UNPROVISIONED_STATUSES
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
    b.button(text="📅 روند فروش اخیر", callback_data="admin:trend")
    b.button(text="📊 فروش به تفکیک پلن", callback_data="admin:plans")
    b.button(text="🧭 وضعیت سفارش‌ها", callback_data="admin:funnel")
    b.button(text="👥 فروش به تفکیک خریدار", callback_data="admin:buyers")
    b.adjust(1)
    return b.as_markup()


def _report_menu():
    b = InlineKeyboardBuilder()
    b.button(text="🔙 بازگشت به پنل", callback_data="admin:home")
    return b.as_markup()


def _paid_row(rows):
    """Yield rows for sold, priced, non-test orders with (plan, price) resolved."""
    for r in rows:
        if r["plan_id"] == TEST_PLAN_ID:
            continue
        plan = settings.get_plan(r["plan_id"])
        if not plan:
            continue
        # Coupon orders count at the amount actually paid, not the list price.
        price = r.get("final_price")
        if price is None:
            price = int(plan["price"])
        yield r, plan, int(price)


def _sold_lines(rows):
    """Yield (telegram_id, username, price, bytes) for priced, non-test orders.

    Free test plans and unknown plan ids carry no price, so they are left out
    of the money/volume totals.
    """
    for r, plan, price in _paid_row(rows):
        yield (
            r["telegram_id"],
            r.get("telegram_username"),
            price,
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


def _plans_report(rows) -> str:
    agg: dict[str, dict] = {}
    for r, plan, price in _paid_row(rows):
        e = agg.setdefault(
            r["plan_id"], {"name": plan["name"], "orders": 0, "price": 0}
        )
        e["orders"] += 1
        e["price"] += price
    test_orders = sum(1 for r in rows if r["plan_id"] == TEST_PLAN_ID)
    if not agg and not test_orders:
        return "📊 هنوز فروشی ثبت نشده است."
    header = "📊 <b>فروش به تفکیک پلن</b>"
    lines = []
    for plan_id, e in sorted(agg.items(), key=lambda kv: kv[1]["price"], reverse=True):
        plan = settings.get_plan(plan_id)
        size = int(plan.get("data_limit_bytes") or 0)
        size_str = "نامحدود" if size == 0 else f"{size / GB:,.0f} گیگ"
        lines.append(
            f"▫️ <b>{e['name']}</b> ({size_str}) — {e['orders']} سفارش — "
            f"{e['price']:,} {settings.CURRENCY}"
        )
    header += "\n" + "\n".join(lines)
    if test_orders:
        header += f"\n<i>(+{test_orders} پلن تستی رایگان)</i>"
    return header


def _period_line(rows, days: int) -> str:
    """One 'N روز اخیر' summary line from sold, priced, non-test orders."""
    cutoff = datetime.now() - timedelta(days=days)
    mine = [
        (r, plan, price)
        for r, plan, price in _paid_row(rows)
        if r["created_at"] and r["created_at"] >= cutoff
    ]
    label = "۷ روز اخیر" if days == 7 else f"{days} روز اخیر"
    if not mine:
        return f"▫️ {label}: سفارشی ثبت نشده"
    price = sum(p for _, _, p in mine)
    bytes_ = sum(int(plan.get("data_limit_bytes") or 0) for _, plan, _ in mine)
    size_str = f"{bytes_ / GB:,.1f} گیگ" if bytes_ else "نامحدود"
    return f"▫️ {label}: {len(mine)} سفارش — {size_str} — {price:,} {settings.CURRENCY}"


def _trend_report(rows) -> str:
    if not any(True for _ in _paid_row(rows)):
        return "📅 هنوز فروشی ثبت نشده است."
    return (
        "📅 <b>روند فروش اخیر</b>\n\n"
        + _period_line(rows, 7)
        + "\n"
        + _period_line(rows, 30)
    )


def _funnel_report(all_rows) -> str:
    """Lifecycle analytics over ALL orders (pending/rejected included)."""
    if not all_rows:
        return "🧭 هنوز سفارشی ثبت نشده است."
    unprov = set(UNPROVISIONED_STATUSES)
    pending = sum(1 for r in all_rows if r["status"] in {"pending"})
    rejected = sum(1 for r in all_rows if r["status"] in {"rejected"})
    sold_rows = [r for r in all_rows if r["status"] not in unprov]
    purchases = sum(1 for r in sold_rows if r["order_type"] != "renewal")
    renewals = len(sold_rows) - purchases

    coupon_orders = 0
    coupon_discount = 0
    revenue = 0
    for r, plan, price in _paid_row(sold_rows):
        coupon = r.get("coupon_code")
        if coupon and r.get("final_price") is not None:
            coupon_orders += 1
            discount = int(plan["price"]) - price
            if discount > 0:
                coupon_discount += discount
        revenue += price
    avg = revenue // len(sold_rows) if sold_rows else 0
    conv = len(sold_rows) * 100 // len(all_rows) if all_rows else 0

    return (
        "🧭 <b>وضعیت سفارش‌ها</b>\n\n"
        f"▫️ کل سفارش‌ها: <b>{len(all_rows)}</b>\n"
        f"▫️ در انتظار بررسی: <b>{pending}</b>\n"
        f"▫️ رد شده: <b>{rejected}</b>\n"
        f"▫️ فروخته‌شده: <b>{len(sold_rows)}</b> ({conv}٪ تبدیل)\n"
        f"▫️ خرید / تمدید: <b>{purchases}</b> / <b>{renewals}</b>\n"
        f"▫️ سفارش با کد تخفیف: <b>{coupon_orders}</b>"
        f" — مجموع تخفیف: <b>{coupon_discount:,} {settings.CURRENCY}</b>\n"
        f"▫️ میانگین هر سفارش: <b>{avg:,} {settings.CURRENCY}</b>"
    )


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


@router.callback_query(F.data == "admin:plans")
async def cb_admin_plans(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows = await db.get_sales_data()
    await safe_edit_text(
        callback.message, _plans_report(rows), reply_markup=_report_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "admin:trend")
async def cb_admin_trend(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows = await db.get_sales_data()
    await safe_edit_text(
        callback.message, _trend_report(rows), reply_markup=_report_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "admin:funnel")
async def cb_admin_funnel(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows = await db.get_all_orders_for_admin()
    await safe_edit_text(
        callback.message, _funnel_report(rows), reply_markup=_report_menu()
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
