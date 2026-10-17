from typing import List
from celery import shared_task
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from html import escape
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
# local
from database import SessionLocal, Order, NotificationLog, NotificationKind, NotificationStatus
from models import UNPROVISIONED_STATUSES, PANEL_STATUS_TO_ORDER_STATUS
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from config import settings
from panel_api import panel, UserStatus
from handlers.test_plan import TEST_PLAN_ID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status sync. The panel is the source of truth: every provisioned order's
# status is copied verbatim from the panel (no recomputation from traffic or
# expiry, so precedence and unlimited plans are handled by the panel itself).
# ---------------------------------------------------------------------------
async def _fetch_provisioned_orders() -> List[Order]:
    # Orders without a panel_username are manual-tier (eco/unlimited): the
    # admin manages them on the panel by hand, so they have nothing to sync.
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order).where(
                Order.status.notin_(UNPROVISIONED_STATUSES),
                Order.panel_username.isnot(None),
            )
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
                # Free test plans never get expiry reminders.
                Order.plan_id != TEST_PLAN_ID,
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


async def _send_telegram_message(telegram_id: int, message: str, *args, reply_markup=None, **kwargs):
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    async with bot:
        await bot.send_message(
            chat_id=telegram_id,
            text=message,
            reply_markup=reply_markup,
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


def _sub_title(order) -> str:
    """User-facing subscription title for reminder texts: the plan's
    nickname-free display name. Falls back to the panel username only if
    the configured plan has been deleted from plans.json."""
    plan = settings.get_plan(order.plan_id)
    if plan:
        return plan["name"]
    return order.panel_username or "شما"


@shared_task(
    bind=True,
    max_retries=3,
    # autoretry_for=(Exception,)
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
        ).format(_sub_title(order))

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
                # Free test plans never get usage reminders.
                Order.plan_id != TEST_PLAN_ID,
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
            ).format(_sub_title(order))

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


# --- Never-used onboarding nudge -------------------------------------------
#
# A freshly provisioned panel account reports status="active" even before the
# user ever connects, so panel status cannot be used to detect "not active".
# The only reliable signal is traffic: a subscription that has never been used
# carries used_traffic == 0. We nudge those accounts once, while they are still
# usable (not expired, limit not reached, and the panel does not report them as
# limited/expired/disabled).
#
# Delivery is strictly once per subscription cycle: the
# (order_id, UNUSED_YET, expire_at) row in notification_log is claimed
# atomically, so a second scan can never send a duplicate. Orders younger than
# UNUSED_NUDGE_MIN_AGE are skipped so the user gets a chance to open the
# subscription on their own first.
UNUSED_NUDGE_MIN_AGE = timedelta(minutes=15)

_PANEL_DEAD_STATUSES = {"limited", "expired", "disabled", "on_hold"}


def _utcnow_naive() -> datetime:
    """Naive UTC, matching the clock SQLite uses for server-stamped columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_unused(user: dict | None) -> bool:
    """True when the panel says this account was never used and is still usable.

    "Never used" is read from the panel's last-seen field (`online_at`), which
    the dashboard shows as "never"; zero traffic is required as well.
    """
    if not user:
        return False
    try:
        if str(user.get("status") or "").lower() in _PANEL_DEAD_STATUSES:
            return False
        if user.get("online_at"):
            return False
        if int(user.get("used_traffic") or 0) != 0:
            return False
        expire = user.get("expire")
        if expire and float(expire) <= time.time():
            return False
        return int(user.get("data_limit") or 0) > 0
    except (TypeError, ValueError):
        return False


def _unused_nudge_text(panel_username: str) -> str:
    return (
        f"از اشتراک شما با نام کاربری <code>{escape(panel_username)}</code> "
        "هنوز استفاده‌ای نشده.\n\n"
        "برای اتصال، راهنمای زیر را ببینید."
    )


def _unused_nudge_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="آموزش استفاده", callback_data="menu:docs")
        ]]
    )


async def _fetch_unused_candidates(now: datetime, cutoff_utc: datetime) -> list[Order]:
    # Expiry is decided by the panel (see _is_unused); the DB expiry can be
    # stale or null, so it is deliberately not part of this query.
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order).where(
                Order.plan_id == TEST_PLAN_ID,
                Order.status.notin_(UNPROVISIONED_STATUSES),
                Order.panel_username.isnot(None),
                Order.created_at <= cutoff_utc,
            )
        )
        return list(result.scalars().all())


async def _unused_funnel_counts(cutoff_utc: datetime) -> tuple[int, int, int, int]:
    """How many test orders survive each filter — for diagnosing empty scans."""
    plan = Order.plan_id == TEST_PLAN_ID
    provisioned = Order.status.notin_(UNPROVISIONED_STATUSES)
    named = Order.panel_username.isnot(None)
    async with SessionLocal() as session:
        async def count(*where):
            return (
                await session.execute(
                    select(func.count()).select_from(Order).where(plan, *where)
                )
            ).scalar()
        return (
            await count(),
            await count(provisioned),
            await count(provisioned, named),
            await count(provisioned, named, Order.created_at <= cutoff_utc),
        )


def _run_unused_nudge():
    """Nudge never-used test accounts exactly once. Safe to run on a schedule."""
    now = datetime.now()
    cutoff_utc = _utcnow_naive() - UNUSED_NUDGE_MIN_AGE
    try:
        orders = asyncio.run(_fetch_unused_candidates(now, cutoff_utc))
    except Exception as e:
        logger.exception("Unused nudge: DB scan failed: %s", e)
        return None
    if not orders:
        try:
            total, provisioned, named, aged = asyncio.run(
                _unused_funnel_counts(cutoff_utc)
            )
            logger.info(
                "Unused nudge: no candidates (test-plan orders total=%d, "
                "provisioned=%d, with panel_username=%d, older than %s=%d)",
                total, provisioned, named, UNUSED_NUDGE_MIN_AGE, aged,
            )
        except Exception as e:
            logger.warning("Unused nudge: funnel counts failed: %s", e)
        return None
    try:
        panel_users = asyncio.run(panel.get_users_list())
    except Exception as e:
        logger.exception("Unused nudge: panel fetch failed: %s", e)
        return None
    by_username = {
        u.get("username"): u for u in panel_users if u.get("username")
    }
    sent = skipped = missing = 0
    for order in orders:
        user = by_username.get(order.panel_username)
        if user is None:
            missing += 1
            continue
        if not _is_unused(user):
            skipped += 1
            continue
        cycle = order.expire_at or now
        if not asyncio.run(
            _claim_notification(order.order_id, NotificationKind.UNUSED_YET, cycle)
        ):
            continue
        try:
            asyncio.run(
                _send_telegram_message(
                    order.telegram_id,
                    _unused_nudge_text(order.panel_username),
                    reply_markup=_unused_nudge_keyboard(),
                )
            )
        except Exception as e:
            logger.warning(
                "Unused nudge: send failed for order %s: %s", order.order_id, e
            )
            asyncio.run(
                _mark_notification(
                    order.order_id, NotificationKind.UNUSED_YET, cycle,
                    NotificationStatus.FAILED,
                )
            )
            continue
        sent += 1
        asyncio.run(
            _mark_notification(
                order.order_id, NotificationKind.UNUSED_YET, cycle,
                NotificationStatus.SENT,
            )
        )
        logger.info(
            "Unused nudge sent for order %s (%s)", order.order_id, order.panel_username
        )
    logger.info(
        "Unused nudge: candidates=%d sent=%d skipped_used=%d panel_missing=%d",
        len(orders), sent, skipped, missing,
    )
    return None


@shared_task(
    bind=True,
    max_retries=1,
)
def send_unused_test_nudge(*args, **kwargs):
    """Celery entrypoint for the never-used onboarding nudge."""
    return _run_unused_nudge()
