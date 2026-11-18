from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from aiogram.filters import BaseFilter

import database as db
from config import settings
from panel_api import panel, PanelAPIError
from services.subscriptions.base import SubscriptionError
from services.subscriptions.provisioning import approve_purchase, approve_renewal
from services.subscriptions.registry import provider_name_for_plan
from utils import make_panel_username, safe_edit_caption

router = Router()

from logging import getLogger

logger = getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_USER_IDS


async def _free_panel_username(base: str) -> str:
    """Return `base`, or the first numbered variant (base_2, base_3, ...) the
    panel doesn't have yet.

    Different buyers never collide (their Telegram id is on the name); the
    same buyer requesting the same name twice did, which left the second
    order stuck in pending. `get_user` returns None on 404 and raises
    PanelAPIError on real failures, which the caller handles.
    """
    candidate = base
    n = 1
    while await panel.get_user(candidate):
        n += 1
        candidate = f"{base}_{n}"
        if n > 20:
            raise PanelAPIError(f"can't find a free username variant for {base!r}")
    return candidate


class ReplyToOrder(BaseFilter):
    """Matches admin messages replying to an order receipt in the admin group
    (orders store their admin message id). Support replies target bot-copied
    user messages, which are not orders, so they fall through to docs_support.
    """

    async def __call__(self, message: Message) -> bool:
        return bool(
            await db.get_order_by_admin_message(message.reply_to_message.message_id)
        )


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
    is_renewal = order.get("order_type") == "renewal"

    if is_renewal:
        target_provider = order.get("target_provider") or (
            "vip_panel" if order.get("target_panel_username") else None
        )
        if target_provider == "dante_api":
            try:
                result = await approve_renewal(order, plan)
            except SubscriptionError as e:
                await safe_edit_caption(
                    callback.message,
                    caption=callback.message.caption
                    + f"\n\n⚠️ خطای سرویس: {e}",
                )
                await callback.answer(
                    "تمدید سرویس ناموفق بود — خطا را در بالا ببینید.",
                    show_alert=True,
                )
                return

            await bot.send_message(
                chat_id=telegram_id,
                text=(
                    "🎉 <b>تمدید با موفقیت انجام شد!</b>\n\n"
                    f"پلن جدید: {plan['name']}\n\n"
                    f"لینک اشتراک شما تغییری نکرده و همچنان معتبر است:\n"
                    f"<code>{result.user.subscription_url or 'نامشخص'}</code>"
                ),
            )
            await safe_edit_caption(
                callback.message,
                caption=callback.message.caption
                + f"\n\n✅ تمدید شد — سرویس به‌صورت خودکار تمدید شد.",
            )
            await callback.answer("تایید شد")
            return

        panel_username = order["target_panel_username"]
        try:
            user_obj = await panel.renew_user(
                username=panel_username,
                data_limit_bytes=plan["data_limit_bytes"],
                duration_days=plan["duration_days"],
            )
        except PanelAPIError as e:
            await safe_edit_caption(callback.message, 
                caption=callback.message.caption + f"\n\n⚠️ خطای پنل: {e}"
            )
            await callback.answer("تمدید سرویس ناموفق بود — خطا را در بالا ببینید.", show_alert=True)
            return

        await db.link_panel_account(telegram_id, panel_username)

        # Align this order's expiry with the panel's real one (renew_user extends
        # from approval time, not from when the receipt was sent), and retire the
        # previous order row for this panel account so its stale expiry can never
        # trigger another reminder for a subscription that was just extended.
        # Both writes happen in one transaction so a scan can never see the new
        # order approved while the old one is still eligible.
        expire_ts = user_obj.get("expire")
        if expire_ts:
            expire_at = datetime.fromtimestamp(float(expire_ts))
        else:
            expire_at = datetime.now() + timedelta(days=int(plan["duration_days"]))
        await db.approve_renewal_order(order_id, panel_username, expire_at)

        sub_url = user_obj.get("subscription_url", "نامشخص")

        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🎉 <b>تمدید با موفقیت انجام شد!</b>\n\n"
                f"پلن جدید: {plan['name']}\n\n"
                f"لینک اشتراک شما تغییری نکرده و همچنان معتبر است:\n"
                f"<code>{sub_url}</code>"
            ),
        )
        await safe_edit_caption(callback.message, 
            caption=callback.message.caption + f"\n\n تایید شد توسط {callback.from_user.first_name}"
        )
        await callback.answer("تایید شد")
        return
    else:
        provider_name = provider_name_for_plan(plan) if plan else None
        if provider_name == "dante_api":
            try:
                result = await approve_purchase(order, plan)
            except SubscriptionError as e:
                await safe_edit_caption(
                    callback.message,
                    caption=callback.message.caption
                    + f"\n\n⚠️ خطای سرویس: {e}",
                )
                await callback.answer(
                    "ایجاد/تمدید سرویس ناموفق بود — خطا را در بالا ببینید.",
                    show_alert=True,
                )
                return

            action = "تمدید شد" if result.extended else "آماده است"
            await bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"🎉 <b>سرویس شما {action}!</b>\n\n"
                    f"پلن: {plan['name']}\n\n"
                    f"لینک اشتراک:\n"
                    f"<code>{result.user.subscription_url or 'نامشخص'}</code>\n\n"
                    "برای راهنمای اتصال، /start → 📖 راهنمای استفاده را بزنید."
                ),
            )
            await safe_edit_caption(
                callback.message,
                caption=callback.message.caption
                + f"\n\n✅ تایید شد — سرویس به‌صورت خودکار {action}.",
            )
            await callback.answer("تایید شد")
            return

        is_manual = bool(plan and plan.get("manual"))
        if is_manual:
            # Legacy manual providers: the admin builds the account outside
            # the bot and delivers the link by replying to this receipt.
            expire_at = datetime.now() + timedelta(days=int(plan["duration_days"]))
            await db.set_order_status(order_id, "approved")
            await db.set_order_expire_at(order_id, expire_at)

            await bot.send_message(
                chat_id=telegram_id,
                text=(
                    "✅ <b>پرداخت شما تایید شد!</b>\n\n"
                    f"پلن: {plan['name']}\n\n"
                    "سرویس شما در حال آماده‌سازی است و لینک اکانت "
                    "به‌زودی در همین چت ارسال می‌شود."
                ),
            )
            await safe_edit_caption(callback.message, 
                caption=callback.message.caption
                + "\n\n✅ تایید شد — پس از ساخت اکانت، لینک اشتراک را به همین پیام ریپلای کنید."
            )
            await callback.answer("تایید شد — لینک را بعداً ریپلای کنید")
            return

        panel_username = await db.get_panel_username(order_id)

        try:
            if not panel_username:
                base = make_panel_username(order["requested_username"], telegram_id)
                # Two purchases with the same requested name would collide on
                # create_user, so probe the panel and pick the first free variant.
                panel_username = await _free_panel_username(base)
                logger.info("Panel username for order %s: %s", order_id, panel_username)

            user_obj = await panel.create_user(
                username=panel_username,
                telegram_id=telegram_id,
                data_limit_bytes=plan["data_limit_bytes"],
                duration_days=plan["duration_days"],
            )
        except PanelAPIError as e:
            await safe_edit_caption(callback.message, 
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
        await safe_edit_caption(callback.message, 
            caption=callback.message.caption + f"\n\n تایید شد توسط {callback.from_user.first_name}"
        )
        await callback.answer("تایید شد")
        return

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

    await safe_edit_caption(callback.message, 
        caption=callback.message.caption + f"\n\n❌ رد شد توسط {callback.from_user.first_name}"
    )
    await callback.answer("رد شد.")


_LINK_RE = r"(https?://\S+|sub[a-z]*://\S+)"


@router.message(
    F.chat.id == settings.ADMIN_CHAT_ID,
    F.reply_to_message,
    F.text,
    ReplyToOrder(),
)
async def receive_manual_link(message: Message, bot: Bot):
    """Manual-tier delivery: the admin creates the account on the panel (or
    any infra) and replies to the order receipt with the subscription URL.
    The bot stores it on the order and forwards it to the buyer."""
    if not _is_admin(message.from_user.id):
        return

    order = await db.get_order_by_admin_message(message.reply_to_message.message_id)
    if not order:
        return

    if order["status"] == "pending":
        await message.reply("⚠️ این سفارش هنوز تایید نشده است.")
        return
    if order.get("panel_username") or order.get("provider_user_id"):
        await message.reply("ℹ️ این سفارش خودکار ایجاد شده است و به لینک دستی نیازی ندارد.")
        return

    import re

    match = re.search(_LINK_RE, message.text)
    if not match:
        await message.reply("⚠️ لینک اشتراک پیدا نشد — لینک باید با http/https شروع شود.")
        return
    manual_link = match.group(1)

    plan = settings.get_plan(order.get("plan_id") or "")
    plan_name = plan["name"] if plan else "-"
    await db.set_manual_link(order["order_id"], manual_link)

    caption = message.reply_to_message.caption or ""
    if "لینک ارسال شد" not in caption:
        try:
            await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=message.reply_to_message.message_id,
                caption=caption + "\n\n🔗 لینک ارسال شد.",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise

    await bot.send_message(
        chat_id=order["telegram_id"],
        text=(
            "🎉 <b>سرویس شما آماده شد!</b>\n\n"
            f"پلن: {plan_name}\n\n"
            f"لینک اشتراک:\n<code>{manual_link}</code>\n\n"
            "این لینک را در اپلیکیشن خود وارد کنید. "
            "برای راهنمای اتصال، /start → 📖 راهنمای استفاده را بزنید."
        ),
    )
    await message.reply("✅ لینک برای مشتری ارسال شد.")
