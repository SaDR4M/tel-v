from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from config import settings
from utils import make_panel_username
from handlers.keyboards import plans_menu, confirm_purchase, admin_review_keyboard, back_home
from panel_api import panel, PanelAPIError

router = Router()


class PurchaseFlow(StatesGroup):
    waiting_for_username = State()
    waiting_for_receipt = State()


# @router.callback_query(F.data == "menu:plans")
@router.message(F.text == "خرید اشتراک جدید")
async def cb_plans(callback: CallbackQuery):
    await callback.answer(
        "📋 <b>پلن‌های موجود</b>\n\nیکی را انتخاب کنید:",
        reply_markup=plans_menu(),
    )


@router.message(Command("plans"))
async def cmd_plans(message: Message):
    await message.answer(
        "📋 <b>پلن‌های موجود</b>\n\nیکی را انتخاب کنید:",
        reply_markup=plans_menu(),
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    plan_id = callback.data.split(":", 1)[1]
    plan = settings.get_plan(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد.", show_alert=True)
        return

    data_str = 0 if plan["data_limit_bytes"] == 0 else f"{plan['data_limit_bytes'] / 1_073_741_824:.0f} گیگابایت"
    text = (
        f"🧾 <b>{plan['name']}</b>\n\n"
        f"قیمت: <b>{plan['price']} {settings.CURRENCY}</b>\n"
        f"حجم: {data_str}\n"
        f"مدت: {plan['duration_days']} روز\n\n"
        f"مبلغ را به مشخصات زیر واریز کنید:\n\n"
        f"💳 : <b>6219 8610 8648 5009</b>\n"
        f"👤 : <b>صدرا آقاجانلو</b>\n\n"
        f"پس از واریز، رسید پرداخت را ارسال کنید."
    )
    await callback.message.edit_text(text, reply_markup=confirm_purchase(plan_id))
    await callback.answer()


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
    if not plan or not panel_username:
        await message.answer("مشکلی پیش آمد، لطفاً دوباره با /start شروع کنید.")
        await state.clear()
        return

    file_id = message.photo[-1].file_id
    order_id = await db.create_order(message.from_user.id, plan_id, requested_username, file_id)
    await state.clear()

    uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    caption = (
        f"🆕 <b>سفارش جدید #{order_id}</b>\n"
        f"کاربر: {uname} (شناسه: <code>{message.from_user.id}</code>)\n"
        f"نام درخواستی: {requested_username}\n"
        f"نام کاربری پنل: <code>{panel_username}</code>\n"
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
        "✅ رسید دریافت شد! یک ادمین به‌زودی آن را بررسی می‌کند. "
        "پس از تایید، همینجا پیامی دریافت خواهید کرد."
    )


@router.message(PurchaseFlow.waiting_for_receipt)
async def receive_receipt_wrong_type(message: Message):
    await message.answer("لطفاً رسید را به‌صورت عکس/اسکرین‌شات ارسال کنید.")
