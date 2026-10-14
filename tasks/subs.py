from typing import List
from celery import shared_task
import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
# local
from database import SessionLocal, Order, NotificationLog, NotificationKind, NotificationStatus
from models import UNPROVISIONED_STATUSES, PANEL_STATUS_TO_ORDER_STATUS
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from config import settings
from panel_api import panel, UserStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status sync. The panel is the source of truth: every provisioned order's
# status is copied verbatim from the panel (no recomputation from traffic or
# expiry, so precedence and unlimited plans are handled by the panel itself).
# ---------------------------------------------------------------------------
async def _fetch_provisioned_orders() -> List[Order]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order).where(Order.status.notin_(UNPROVISIONED_STATUSES))
        )
        return result.scalars().all()


def _panel_statuses_by_username(users: List[dict]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for user in users:
        panel_username = user.get("username")
        if panel_username:
            statuses[panel_username] = (user.get("status") or "").lower()
    return statuses


async def _order_status_bulk_update(orders: List[Order]):
    if not orders:
        return

    updates = [
        {
            "order_id": order.order_id,
            "status": order.status,
        }
        for order in orders
    ]
    logger.debug("Syncing statuses: %s", updates)

    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(Order),
                updates,
            )


@shared_task(
    bind=True,
    max_retries=3,
    # autoretry_for((Exception,))
)
def sync_order_statuses(*args, **kwargs):
    """Mirror every provisioned order's status from the panel.

    NOTE: This task runs before the reminder tasks so they see fresh statuses.
    """
    orders = asyncio.run(_fetch_provisioned_orders())
    if not orders:
        return 0

    all_users = asyncio.run(panel.get_users_list(status=None))
    panel_statuses = _panel_statuses_by_username(all_users)

    to_update: List[Order] = []
    for order in orders:
        panel_status = panel_statuses.get(order.panel_username)
        if panel_status is None:
            logger.warning(
                "Order %s: panel user '%s' not found, status left unchanged.",
                order.order_id,
                order.panel_username,
            )
            continue

        new_status = PANEL_STATUS_TO_ORDER_STATUS.get(panel_status)
        if new_status is not None and new_status != order.status:
            order.status = new_status
            to_update.append(order)

    asyncio.run(_order_status_bulk_update(to_update))
    logger.info("Status sync: %d order(s) updated.", len(to_update))
    return len(to_update)

# ------------------------------------------------------
async def _scan_subscriptions_expire_date():
    now = datetime.now()
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order).where(
                Order.status.notin_(UNPROVISIONED_STATUSES),
                Order.expire_at > now,
                Order.expire_at < now + timedelta(days=2)
            )
        )
        orders = result.scalars().all()
        return orders


async def _claim_notification(
    order_id: int,
    kind: NotificationKind,
    cycle_expires_at: datetime,
) -> bool:
    """Atomically claim the (order, kind, cycle) notification slot.

    The unique constraint on notification_log is used as the lock: the first
    caller inserts a PENDING row and gets True; a concurrent/second caller hits
    IntegrityError and gets False, so a message is never sent twice.
    """
    async with SessionLocal() as session:
        session.add(
            NotificationLog(
                order_id=order_id,
                kind=kind,
                status=NotificationStatus.PENDING,
                cycle_expires_at=cycle_expires_at,
            )
        )
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def _mark_notification(
    order_id: int,
    kind: NotificationKind,
    cycle_expires_at: datetime,
    status: NotificationStatus,
):
    values = {"status": status}
    if status == NotificationStatus.SENT:
        values["sent_at"] = datetime.now()
    async with SessionLocal() as session:
        await session.execute(
            update(NotificationLog)
            .where(
                NotificationLog.order_id == order_id,
                NotificationLog.kind == kind,
                NotificationLog.cycle_expires_at == cycle_expires_at,
            )
            .values(**values)
        )
        await session.commit()


async def _send_telegram_message(telegram_id: int, message: str, *args, **kwargs):
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    async with bot:
        await bot.send_message(
            chat_id=telegram_id,
            text=message,
            request_timeout=60
        )
            


@shared_task(
    bind=True,
    max_retries=1,
    # autoretry_for=(Exception,)
)
def scan_subscriptions(*args, **kwargs):
    """Run both reminder passes.

    Order statuses are kept fresh by the separate `sync-order-status` beat
    schedule. These reminders don't depend on that mirrored status (the expiry
    pass reads Order.expire_at and only excludes pending/rejected orders; the
    usage pass reads live panel data), so we don't re-sync here.
    """
    for task in (send_expire_reminder, send_limit_reminder):
        try:
            task()
        except Exception:
            logger.exception("Reminder pipeline step %s failed.", task.name)
    return None


@shared_task(
    bind=True,
    max_retries=3,
    # autoretry_for=(Exception,),
)
def send_expire_reminder(*args, **kwargs):
    orders = asyncio.run(_scan_subscriptions_expire_date())
    for order in orders:
        cycle_expires_at = order.expire_at
        # Claim before sending so an overlapping beat run can't double-send.
        if not asyncio.run(
            _claim_notification(order.order_id, NotificationKind.EXPIRY_2D, cycle_expires_at)
        ):
            continue

        text = (
            "⚠️ اشتراک «{}» شما در آستانه‌ی پایان است.\n"
            "برای جلوگیری از قطع شدن سرویس، لطفاً اشتراک خود را تمدید کنید."
        ).format(order.panel_username)

        try:
            asyncio.run(_send_telegram_message(order.telegram_id, text))
        except Exception as e:
            logger.warning(
                "Expiry reminder failed for order %s: %s", order.order_id, e
            )
            asyncio.run(
                _mark_notification(
                    order.order_id, NotificationKind.EXPIRY_2D,
                    cycle_expires_at, NotificationStatus.FAILED,
                )
            )
            continue

        asyncio.run(
            _mark_notification(
                order.order_id, NotificationKind.EXPIRY_2D,
                cycle_expires_at, NotificationStatus.SENT,
            )
        )

    return None

# ---------------------------------------------------------------------------- 
async def _fetch_order(panel_username: str):
    """Newest provisioned order for a panel username.

    Renewals reuse the same panel username, so match on it alone and take the
    latest expiry; a date-equality match would both miss and raise
    MultipleResultsFound.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order)
            .where(
                Order.panel_username == panel_username,
                Order.status.notin_(UNPROVISIONED_STATUSES),
            )
            .order_by(Order.expire_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _scan_subscriptions_limit_usage():
    # Call the panel api and fetch all active orders, then check each limit usage.
    return await panel.get_users_list(status=UserStatus.ACTIVE.value)


@shared_task(
    bind=True,
    max_retries=3,
    # autoretry_for=(Exception,)
)
def send_limit_reminder(*args, **kwargs):
    """Remind users who passed 90% of their plan's data limit."""
    try:
        users = asyncio.run(_scan_subscriptions_limit_usage())
    except Exception:
        logger.exception("Usage reminder: failed to fetch panel users.")
        return None

    for user in users:
        try:
            data_limit = user.get("data_limit")
            used_traffic = user.get("used_traffic") or 0
            panel_username = user.get("username")

            # 0/None data_limit means an unlimited plan -- nothing to warn about.
            if not data_limit or not panel_username:
                continue

            used_percentage = (used_traffic / data_limit) * 100
            if used_percentage <= 90:
                continue

            order = asyncio.run(_fetch_order(panel_username))
            if not order:
                logger.warning(
                    "Usage reminder: no provisioned order for panel user '%s'.",
                    panel_username,
                )
                continue

            cycle_expires_at = order.expire_at
            if not asyncio.run(
                _claim_notification(
                    order.order_id, NotificationKind.USAGE_90, cycle_expires_at
                )
            ):
                continue

            text = (
                "⚠️ بیش از ۹۰٪ از حجم اشتراک «{}» شما مصرف شده است.\n"
                "برای جلوگیری از قطع شدن سرویس، لطفاً نسبت به تمدید یا خرید سرویس جدید اقدام کنید."
            ).format(order.panel_username)

            try:
                asyncio.run(_send_telegram_message(order.telegram_id, text))
            except Exception as e:
                logger.warning(
                    "Usage reminder failed for order %s: %s", order.order_id, e
                )
                asyncio.run(
                    _mark_notification(
                        order.order_id, NotificationKind.USAGE_90,
                        cycle_expires_at, NotificationStatus.FAILED,
                    )
                )
                continue

            asyncio.run(
                _mark_notification(
                    order.order_id, NotificationKind.USAGE_90,
                    cycle_expires_at, NotificationStatus.SENT,
                )
            )
        except Exception as e:
            logger.exception(
                "Usage reminder: unexpected error for user %s: %s",
                user.get("username"), e,
            )

    return None
