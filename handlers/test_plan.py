from utils import safe_edit_text

import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

import database as db
from panel_api import panel, PanelAPIError
from datetime import timedelta, datetime

router = Router()
logger = logging.getLogger(__name__)

TEST_PLAN_ID = "test_1d_250mb"
TEST_DATA_LIMIT_BYTES = 250 * 1024 * 1024  # 250 MB
TEST_DURATION_DAYS = 7


@router.message(F.text == "اکانت تست")
async def cb_apply_test(message: Message):
    # One test per Telegram account, ever.
    already_used = await db.has_used_plan(message.from_user.id, TEST_PLAN_ID)
    if already_used:
        await message.answer(
            "شما قبلاً از اشتراک تست رایگان استفاده کرده‌اید. "
            "هر کاربر فقط یک‌بار می‌تواند از این امکان استفاده کند."
        )
        return

    requested_username = message.from_user.username or str(message.from_user.id)
    order_id = await db.create_order(
        telegram_id=message.from_user.id,
        plan_id=TEST_PLAN_ID,
        expire_at=datetime.now() + timedelta(days=TEST_DURATION_DAYS),
        requested_username=requested_username,
        receipt_file_id="test_plan_no_receipt",
    )

    panel_username = f"test_{message.from_user.id}"

    try:
        user_obj = await panel.create_user(
            username=panel_username,
            telegram_id=message.from_user.id,
            data_limit_bytes=TEST_DATA_LIMIT_BYTES,
            duration_days=TEST_DURATION_DAYS,
        )
    except PanelAPIError as e:
        # Log-only detail; the user just sees a friendly message.
        logger.error("Panel error while creating test-plan user: %s", e)
        await message.answer(
            "مشکلی پیش آمد، لطفاً با پشتیبانی تماس بگیرید."
        )
        return

    await db.link_panel_account(message.from_user.id, panel_username)
    await db.set_order_status(order_id, "approved", panel_username)

    sub_url = user_obj.get("subscription_url", "نامشخص")
    result_text = (
        f"🎉 <b>اشتراک تست رایگان شما آماده است!</b>\n\n"
        f"مدت اعتبار: {TEST_DURATION_DAYS} روز — حجم: 250 مگابایت\n\n"
        f"لینک اشتراک:\n<code>{sub_url}</code>\n\n"
        f"برای راهنمای اتصال، دستور /docs را بزنید."
    )

    # reply_markup=None removes the menu buttons entirely, so the chat
    # just shows this plain result — this is the "exit the menu" behavior.
    await message.answer(result_text, reply_markup=None)


@router.callback_query(F.data == "menu:test")
async def cb_apply_menu_test(callback: CallbackQuery):
    # One test per Telegram account, ever.
    already_used = await db.has_used_plan(callback.from_user.id, TEST_PLAN_ID)
    if already_used:
        await callback.answer(
            "شما قبلاً از اشتراک تست رایگان استفاده کرده‌اید. "
            "هر کاربر فقط یک‌بار می‌تواند از این امکان استفاده کند.",
            show_alert=True,
        )
        return

    requested_username = callback.from_user.username or str(callback.from_user.id)
    order_id = await db.create_order(
        telegram_id=callback.from_user.id,
        plan_id=TEST_PLAN_ID,
        expire_at=datetime.now() + timedelta(days=TEST_DURATION_DAYS),
        requested_username=requested_username,
        receipt_file_id="test_plan_no_receipt",
    )

    panel_username = f"test_{callback.from_user.id}"

    try:
        user_obj = await panel.create_user(
            username=panel_username,
            telegram_id=callback.from_user.id,
            data_limit_bytes=TEST_DATA_LIMIT_BYTES,
            duration_days=TEST_DURATION_DAYS,
        )
    except PanelAPIError as e:
        # Log-only detail; the user just sees a friendly alert.
        logger.error("Panel error while creating test-plan user: %s", e)
        await callback.answer(
            "مشکلی پیش آمد، لطفاً با پشتیبانی تماس بگیرید.", show_alert=True
        )
        return

    await db.link_panel_account(callback.from_user.id, panel_username)
    await db.set_order_status(order_id, "approved", panel_username)

    sub_url = user_obj.get("subscription_url", "نامشخص")
    result_text = (
        f"🎉 <b>اشتراک تست رایگان شما آماده است!</b>\n\n"
        f"مدت اعتبار: {TEST_DURATION_DAYS} روز — حجم: 250 مگابایت\n\n"
        f"لینک اشتراک:\n<code>{sub_url}</code>\n\n"
        f"برای راهنمای اتصال، دستور /docs را بزنید."
    )

    # reply_markup=None removes the menu buttons entirely, so the chat
    # just shows this plain result — this is the "exit the menu" behavior.
    await safe_edit_text(callback.message, text=result_text, reply_markup=None)
    await callback.answer()

