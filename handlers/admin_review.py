from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery

import database as db
from config import settings
from panel_api import panel, PanelAPIError
from utils import make_panel_username

router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_USER_IDS


@router.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await callback.answer("شما اجازه این کار را ندارید.", show_alert=True)
        return

    order_id = int(callback.data.split(":", 1)[1])
    order = await db.get_order(order_id)
    if not order or order["status"] != "pending":
        await callback.answer("این سفارش قبلاً بررسی شده است.", show_alert=True)
        return

    plan = settings.get_plan(order["plan_id"])
    telegram_id = order["telegram_id"]

    # Reuse an existing linked panel account if this user already has one
    # (keeps renewals on the same account); otherwise derive the unique
    # username from what they picked during checkout.
    panel_username = await db.get_panel_username(order_id) or make_panel_username(
        order["requested_username"], telegram_id
    )

    try:
        user_obj = await panel.create_user(
            username=panel_username,
            telegram_id=telegram_id,
            data_limit_bytes=plan["data_limit_bytes"],
            duration_days=plan["duration_days"],
        )
    except PanelAPIError as e:
        await callback.message.edit_caption(
            caption=callback.message.caption + f"\n\n⚠️ خطای پنل: {e}"
        )
        await callback.answer("ایجاد کاربر در پنل ناموفق بود — خطا را در بالا ببینید.", show_alert=True)
        return

    await db.link_panel_account(telegram_id, panel_username)
    await db.set_order_status(order_id, "approved", panel_username)

    sub_url = user_obj.get("subscription_url", "نامشخص")
    await bot.send_message(
        chat_id=telegram_id,
        text=(
            f"🎉 <b>اشتراک شما آماده است!</b>\n\n"
            f"پلن: {plan['name']}\n\n"
            f"لینک اشتراک:\n<code>{sub_url}</code>\n\n"
            f"برای راهنمای اتصال، /start → 📖 راهنمای استفاده را بزنید."
        ),
    )

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n✅ تایید شد توسط {callback.from_user.first_name}"
    )
    await callback.answer("تایید شد.")


@router.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await callback.answer("شما اجازه این کار را ندارید.", show_alert=True)
        return

    order_id = int(callback.data.split(":", 1)[1])
    order = await db.get_order(order_id)
    if not order or order["status"] != "pending":
        await callback.answer("این سفارش قبلاً بررسی شده است.", show_alert=True)
        return

    await db.set_order_status(order_id, "rejected")

    await bot.send_message(
        chat_id=order["telegram_id"],
        text="❌ رسید پرداخت شما تایید نشد. اگر فکر می‌کنید اشتباهی رخ داده، با پشتیبانی تماس بگیرید.",
    )

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n❌ رد شد توسط {callback.from_user.first_name}"
    )
    await callback.answer("رد شد.")
