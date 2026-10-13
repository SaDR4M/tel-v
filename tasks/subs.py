from typing import Optional, List
from celery import shared_task
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
# local
from database import SessionLocal, Order, NotificationLog, NotificationKind, NotificationStatus, OrderStatus
from schema import OrderSchema
from sqlalchemy import select, exists, func, update
from config import settings
from panel_api import panel, UserStatus


# -----------------------------------------------------
async def _fetch_all_active_orders(status: OrderStatus = OrderStatus.APPROVED):
    print("Fetching active orders")
    async with SessionLocal() as session:
        result = await session.execute(select(Order).where(Order.status == status))
        orders = result.scalars().all()
        return orders

def _process_users(users: List[dict]) -> dict[str, dict]:
    print("Processing users data")
    processed_users = {}

    for user in users:
        panel_username = user.get("username", "")
        if not panel_username:
            continue

        expire = user.get("expire")
        processed_users[panel_username] = {
            "panel_username": panel_username,
            "expire_at": datetime.fromtimestamp(expire) if expire else None,
            "data_limit": user.get("data_limit") or 0,
            "used_traffic": user.get("used_traffic") or 0,
        }

    return processed_users


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
    print(f"Updating the statuses: {updates}")

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
    """
    Update approved order statuses to have valid order statuses.
    NOTE: This task will be called before sending the user reminder message.
    """
    active_orders = asyncio.run(_fetch_all_active_orders())
    print(active_orders)
    all_users = asyncio.run(panel.get_users_list(status=None))

    processed_users = _process_users(all_users)

    now = datetime.now()

    updated = False
    update_orders: List[Order] = []

    for order in active_orders:
        panel_user = processed_users.get(order.panel_username)

        if not panel_user:
            print(
                f"User with {order.panel_username} was not found "
                "in list of panel users."
            )
            continue

        expire_at = panel_user.get("expire_at")
        data_limit = panel_user.get("data_limit")
        used_traffic = panel_user.get("used_traffic")

        new_status = order.status

        if used_traffic >= data_limit:
            new_status = OrderStatus.LIMITED
        elif expire_at < now:
            new_status = OrderStatus.EXPIRED

        if order.status != new_status:
            order.status = new_status
            update_orders.append(order)

    asyncio.run(_order_status_bulk_update(update_orders))

    return 1

# ------------------------------------------------------
async def _scan_subscriptions_expire_date():
    async with SessionLocal() as session:
        # TODO: Update the user order status when it's reached the limit of usage or time.
        result = await session.execute(
            select(Order).where(
                Order.status == "approved",
                Order.expire_at > datetime.now(),
                Order.expire_at < datetime.now() + timedelta(days=2)
            )
        )
        orders = result.scalars().all()
        # orders_schema = [OrderSchema.model_validate(order).model_dump_json() for order in orders]
        return orders


async def _is_notification_sent(order_id: int, order_expires_at: Optional[datetime] = None, notif_kind: NotificationKind = NotificationKind.EXPIRY_2D):
    async with SessionLocal() as session:
        if notif_kind == NotificationKind.EXPIRY_2D:
            result = select(
                exists()
                .where(
                    NotificationLog.order_id == order_id,
                    NotificationLog.kind == notif_kind,
                    NotificationLog.cycle_expires_at == order_expires_at,
                )
            )
        elif notif_kind == NotificationKind.USAGE_90:
            result = select(
                exists()
                .where(
                    NotificationLog.order_id == order_id,
                    NotificationLog.kind == notif_kind,
                    # NotificationLog.cycle_expires_at == order_expires_at,
                )
            )

        return await session.scalar(result)


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
            


async def _store_notification(order_id: int, expire_at: datetime, kind: NotificationKind, status: NotificationStatus):
    async with SessionLocal() as session:
        # Get the notification, if exists update it otherwise create it.
        result = await session.execute(
            select(NotificationLog)
            .where(
                NotificationLog.order_id == order_id,
                NotificationLog.kind == kind
            ).with_for_update()
        )
        notif = result.scalar_one_or_none()
        if not notif:
            notif_log = NotificationLog(
                order_id=order_id,
                kind=kind,
                status=status,
                cycle_expires_at=expire_at,
                sent_at=datetime.now()
            )
            session.add(notif_log)
        else:
            notif.cycle_expires_at = expire_at
            notif.sent_at = datetime.now()

        await session.commit()


@shared_task(
    bind=True,
    max_retries=1,
    # autoretry_for=(Exception,)
)
def scan_subscriptions(*args, **kwargs):
    print("CALLING THE SCAN SUBS")
    send_expire_reminder.apply_async()
    send_limit_reminder.apply_async()
    return None


@shared_task(
    bind=True,
    max_retries=3,
    # autoretry_for=(Exception,),
)
def send_expire_reminder(*args, **kwargs):
    orders = asyncio.run(_scan_subscriptions_expire_date())
    if orders:
        for order in orders:
            telegram_id = order.telegram_id
            panel_name = order.panel_username
            expire_at = order.expire_at
            order_id = order.order_id
            # Check if we have send the notification messsage or not
            is_sent = asyncio.run(_is_notification_sent(order_id, expire_at, NotificationKind.EXPIRY_2D))
            if is_sent:
                print(f"Notification has been sent for this order with order id: {order_id}")
                continue

            # Send the message to them
            EXPIRY_WARNING_TEXT = (
                "⚠️ اشتراک «{}» شما در آستانه‌ی پایان است.\n"
                "برای جلوگیری از قطع شدن سرویس، لطفاً اشتراک خود را تمدید کنید."
            )

            try:
                asyncio.run(
                    _send_telegram_message(
                        telegram_id=telegram_id, message=EXPIRY_WARNING_TEXT.format(panel_name)
                    )
                )
            except Exception as e:
                print(f"Error while sending telegram message: {str(e)} to {order.telegram_id}, for time limit warning")
                continue

            # Store the message
            try:
                asyncio.run(_store_notification(order_id, expire_at, NotificationKind.EXPIRY_2D, NotificationStatus.SENT))
            except Exception as e:
                print(f"Error occured when trying to save the session, error: {str(e)}")
                continue

    return None

# ---------------------------------------------------------------------------- 
async def _fetch_order(panel_username: str, expire_at: datetime):
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order).where(
                Order.status == "approved",
                Order.panel_username == panel_username,
                func.date(Order.expire_at) == expire_at.date()
            )
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
    """Send reminder to user to renew it's subscriptions if it's reached the 90% of the usage"""
    users = asyncio.run(_scan_subscriptions_limit_usage())

    for user in users:
        used_traffic = user.get("used_traffic")
        data_limit = user.get("data_limit")
        panel_username = user.get("username")
        expire_at = datetime.fromtimestamp(user.get("expire"))

        used_percentage = (used_traffic / data_limit) * 100
        # Send user message if the data limit is more than 90 percentage

        if used_percentage > 90:
            print(f"{panel_username} has reached 90% of usage limit")
            # Fetch the order from db, base on the panel_username and the expire date
            order = asyncio.run(_fetch_order(panel_username=panel_username, expire_at=expire_at))
    
            if not order:
                print(f"Order with {panel_username}, {expire_at} was not found.")
                continue

            is_sent = asyncio.run(_is_notification_sent(order_id=order.order_id, notif_kind=NotificationKind.USAGE_90))
            if is_sent:
                print(f"Notification has been sent for this order with order id: {order.order_id}")
                continue

            # Send the message to them
            USAGE_90_WARNING_TEXT = (
                "⚠️ بیش از ۹۰٪ از حجم اشتراک «{}» شما مصرف شده است.\n"
                "برای جلوگیری از قطع شدن سرویس، لطفاً نسبت به تمدید یا خرید سرویس جدید اقدام کنید."
            )
            try:
                asyncio.run(
                    _send_telegram_message(
                        telegram_id=order.telegram_id, message=USAGE_90_WARNING_TEXT.format(order.panel_username)
                    )
                )
            except Exception as e:
                print(f"Error while sending telegram message: {str(e)} to {order.telegram_id}, for Usage warning")
                continue

            # Store the message
            try:
                asyncio.run(_store_notification(order.order_id, expire_at, NotificationKind.USAGE_90, NotificationStatus.SENT))
            except Exception as e:
                print(f"Error occured when trying to save the session, error: {str(e)}")
                continue


    return None
