from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from config import settings
from utils import make_panel_username
from handlers.keyboards import plans_menu, confirm_purchase, admin_review_keyboard, back_home, MENU_BUTTONS
from panel_api import panel, PanelAPIError
from datetime import datetime, timedelta

router = Router()


class PurchaseFlow(StatesGroup):
    waiting_for_username = State()
    waiting_for_receipt = State()
    waiting_for_coupon = State()


@router.message(F.text == "خرید اشتراک جدید")
async def cb_plans(callback: CallbackQuery):
    await callback.answer(
        "📋 <b>پلن‌های موجود</b>\n\nیکی را انتخاب کنید:",
        reply_markup=plans_menu(),
    )

@router.callback_query(F.data == "menu:plans")
async def menu_plans(callback: CallbackQuery):
    await callback.message.edit_text(
        "📋 <b>پلن‌های موجود</b>\n\nیکی را انتخاب کنید:",
        reply_markup=plans_menu(),
    )
    await callback.answer()

@router.message(Command("plans"))
async def cmd_plans(message: Message):
    await message.answer(
        "📋 <b>پلن‌های موجود</b>\n\nیکی را انتخاب کنید:",
        reply_markup=plans_menu(),
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 1)[1]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return

    # Fresh offer screen: drop any coupon applied to a previous plan.
    await state.clear()
    await state.update_data(plan_id=plan_id)
    await callback.message.edit_text(
        _offer_text(plan), reply_markup=confirm_purchase(plan_id)
    )
    await callback.answer()


def _offer_text(plan: dict, coupon_code: str | None = None) -> str:
    data_str = 0 if plan["data_limit_bytes"] == 0 else f"{plan['data_limit_bytes'] / 1_073_741_824:.0f} گیگابایت"
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
        f"🧾 <b>{plan['name']}</b>\n\n"
        f"{price_line}\n"
        f"حجم: {data_str}\n"
        f"مدت: {plan['duration_days']} روز\n\n"
        f"مبلغ را به مشخصات زیر واریز کنید:\n\n"
        f"💳 : <b>6219 8610 8648 5009</b>\n"
        f"👤 : <b>صدرا آقاجانلو</b>\n\n"
        f"پس از واریز، رسید پرداخت را ارسال کنید."
    )


@router.callback_query(F.data.startswith("coupon:ask:"))
async def cb_coupon_ask(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 2)[2]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return

    await state.update_data(plan_id=plan_id)
    await state.set_state(PurchaseFlow.waiting_for_coupon)
    await callback.message.edit_text(
        "🎟 کد تخفیف را ارسال کنید:",
        reply_markup=back_home(),
    )
    await callback.answer()


@router.message(PurchaseFlow.waiting_for_coupon, F.text)
async def receive_coupon(message: Message, state: FSMContext):
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
        _offer_text(plan, coupon_code),
        reply_markup=confirm_purchase(plan_id, coupon_code),
    )


@router.message(PurchaseFlow.waiting_for_coupon)
async def receive_coupon_wrong_type(message: Message):
    await message.answer("لطفاً کد تخفیف را به‌صورت متن ارسال کنید.")


@router.callback_query(F.data.startswith("coupon:remove:"))
async def cb_coupon_remove(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 2)[2]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return

    await state.update_data(plan_id=plan_id, coupon_code=None)
    await state.set_state(None)
    await callback.message.edit_text(
        _offer_text(plan), reply_markup=confirm_purchase(plan_id)
    )
    await callback.answer("تخفیف حذف شد.")


@router.callback_query(F.data.startswith("payflow:"))
async def cb_payflow(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 1)[1]
    await state.update_data(plan_id=plan_id)
    await state.set_state(PurchaseFlow.waiting_for_username)
    await callback.message.edit_text(
        "✏️ یک نام کاربری برای اشتراک خود انتخاب کنید (فقط حروف انگلیسی و عدد، بدون فاصله).\n\n",
        reply_markup=back_home(),
    )
    await callback.answer()


@router.message(PurchaseFlow.waiting_for_username, F.text)
async def receive_username(message: Message, state: FSMContext):
    raw = message.text.strip()
    if len(raw) < 3:
        await message.answer("خیلی کوتاه است — لطفاً حداقل ۳ کاراکتر ارسال کنید.")
        return
    if len(raw) > 30:
        await message.answer("خیلی طولانی است — لطفاً کمتر از ۳۰ کاراکتر ارسال کنید.")
        return

    # Check username to be not in the commands text
    if raw in MENU_BUTTONS:
        await message.answer("نام کاربری معتبر نمی باشد")
        await state.clear()
        return

    final_username = make_panel_username(raw, message.from_user.id)
    await state.update_data(requested_username=raw, panel_username=final_username)
    await state.set_state(PurchaseFlow.waiting_for_receipt)

    await message.answer(
        f"اشتراک شما با این نام ذخیره خواهد شد: <code>{final_username}</code>\n\n"
        f"📎 لطفاً عکس رسید پرداخت را ارسال کنید.",
        reply_markup=back_home(),
    )


@router.message(PurchaseFlow.waiting_for_username)
async def receive_username_wrong_type(message: Message):
    await message.answer("لطفاً نام کاربری دلخواه خود را به‌صورت متن ارسال کنید.")


@router.message(PurchaseFlow.waiting_for_receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    plan_id = data.get("plan_id")
    requested_username = data.get("requested_username")
    panel_username = data.get("panel_username")
    plan = settings.get_plan(plan_id)
    expire_at = datetime.now() + timedelta(days=29)
    if not plan or not panel_username:
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
    order_id = await db.create_order(telegram_id=message.from_user.id, plan_id=plan_id,  requested_username=requested_username, receipt_file_id=file_id, expire_at=expire_at, coupon_code=coupon_code, final_price=final_price)
    await state.clear()

    uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    coupon_line = (
        f"\n🎟 تخفیف: <code>{coupon_code}</code> — مبلغ پرداختی: <b>{final_price} {settings.CURRENCY}</b>"
        if final_price is not None else ""
    )
    caption = (
        f"🆕 <b>سفارش جدید #{order_id}</b>\n"
        f"کاربر: {uname} (شناسه: <code>{message.from_user.id}</code>)\n"
        f"نام درخواستی: {requested_username}\n"
        f"نام کاربری پنل: <code>{panel_username}</code>\n"
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
        "✅ رسید دریافت شد! یک ادمین به‌زودی آن را بررسی می‌کند. "
        "پس از تایید، همینجا پیامی دریافت خواهید کرد."
    )


@router.message(PurchaseFlow.waiting_for_receipt)
async def receive_receipt_wrong_type(message: Message):
    await message.answer("لطفاً رسید را به‌صورت عکس/اسکرین‌شات ارسال کنید.")
