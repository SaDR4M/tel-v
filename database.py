from sqlalchemy import select, update, bindparam
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config import settings
from models import (
    Base,
    User,
    Order,
    OrderStatus,
    Support,
    NotificationLog,
    NotificationKind,
    NotificationStatus,
    UNPROVISIONED_STATUSES,
)
from datetime import datetime

engine = create_async_engine(f"sqlite+aiosqlite:///{settings.DB_PATH}")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_order_columns)


def _ensure_order_columns(conn):
    """Additively patch columns added after the first deploy.

    create_all never alters existing tables, so without this an upgraded
    database would lack the new columns and every ORM read would fail.
    """
    existing = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(orders)").fetchall()
    }
    if "coupon_code" not in existing:
        conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN coupon_code VARCHAR")
    if "final_price" not in existing:
        conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN final_price INTEGER")


async def upsert_user(telegram_id: int, telegram_username: str | None):
    async with SessionLocal() as session:
        user = await session.get(User, telegram_id)
        if user:
            user.telegram_username = telegram_username
        else:
            user = User(telegram_id=telegram_id, telegram_username=telegram_username)
            session.add(user)
        await session.commit()


async def link_panel_account(telegram_id: int, panel_username: str):
    async with SessionLocal() as session:
        user = await session.get(User, telegram_id)
        if user:
            user.panel_username = panel_username
        else:
            # Defensive: create the user row if it somehow doesn't exist yet
            # (e.g. a handler ran before /start ever fired upsert_user).
            user = User(telegram_id=telegram_id, panel_username=panel_username)
            session.add(user)
        await session.commit()


async def get_panel_username(order_id: int) -> str | None:
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        return order.panel_username if order else None


async def create_order(
    telegram_id: int,
    plan_id: str,
    requested_username: str,
    receipt_file_id: str,
    expire_at: datetime,
    order_type: str = "purchase",
    target_panel_username: str | None = None,
    coupon_code: str | None = None,
    final_price: int | None = None,
) -> int:
    async with SessionLocal() as session:
        order = Order(
            telegram_id=telegram_id,
            plan_id=plan_id,
            requested_username=requested_username,
            receipt_file_id=receipt_file_id,
            status="pending",
            order_type=order_type,
            target_panel_username=target_panel_username,
            coupon_code=coupon_code,
            final_price=final_price,
            expire_at=expire_at
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order.order_id


async def set_order_admin_message(order_id: int, admin_message_id: int):
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order:
            order.admin_message_id = admin_message_id
            await session.commit()


async def get_order(order_id: int) -> dict | None:
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if not order:
            return None
        return {c.name: getattr(order, c.name) for c in order.__table__.columns}


async def set_order_status(order_id: int, status: str, panel_username: str | None = None):
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order:
            order.status = status
            order.panel_username = panel_username
            await session.commit()


async def has_used_plan(telegram_id: int, plan_id: str) -> bool:
    """Returns True if this telegram user already has an order for the
    given plan_id (used to gate one-time offers like a free test plan)."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order.order_id).where(
                Order.telegram_id == telegram_id, Order.plan_id == plan_id
            )
        )
        return result.first() is not None


async def get_active_orders(telegram_id: int) -> list[dict]:
    """Returns this user's provisioned + not-yet-expired orders (i.e.
    subscriptions that were actually created on the panel), most recent
    first. Predicate is payment-oriented so it stays valid after the status
    sync task flips an order to its panel-mirrored status."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order)
            .where(
                Order.telegram_id == telegram_id,
                Order.status.notin_(UNPROVISIONED_STATUSES),
                Order.expire_at >= datetime.now(),
            )
            .order_by(Order.created_at.desc())
        )
        orders = result.scalars().all()
        return [
            {c.name: getattr(order, c.name) for c in order.__table__.columns}
            for order in orders
        ]


async def get_distinct_panel_usernames(telegram_id: int) -> list[str]:
    """Returns the distinct panel usernames this telegram user has ever
    had approved (covers both a purchase's panel_username and a renewal's
    target_panel_username), most recently created first."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order.panel_username, Order.target_panel_username)
            .where(
                Order.telegram_id == telegram_id,
                Order.status.notin_(UNPROVISIONED_STATUSES),
            )
            .order_by(Order.created_at.desc())
        )
        rows = result.all()
        seen: dict[str, None] = {}
        for panel_username, target_panel_username in rows:
            for candidate in (panel_username, target_panel_username):
                if candidate and candidate not in seen:
                    seen[candidate] = None
        return list(seen.keys())


async def get_sales_data() -> list[dict]:
    """Every provisioned order (sold — i.e. past the pending/rejected payment
    stage) with the buyer's Telegram username, for the admin sales reports.
    Pricing lives in plans.json, so only the plan_id is returned."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(
                Order.telegram_id,
                User.telegram_username,
                Order.plan_id,
                Order.coupon_code,
                Order.final_price,
                Order.created_at,
            )
            .outerjoin(User, User.telegram_id == Order.telegram_id)
            .where(Order.status.notin_(UNPROVISIONED_STATUSES))
            .order_by(Order.created_at.asc())
        )
        return [
            {
                "telegram_id": telegram_id,
                "telegram_username": telegram_username,
                "plan_id": plan_id,
                "coupon_code": coupon_code,
                "final_price": final_price,
                "created_at": created_at,
            }
            for telegram_id, telegram_username, plan_id, coupon_code, final_price, created_at in result.all()
        ]


# async def get_provisioned_orders() -> list[tuple[int, str, OrderStatus]]:
#     """Returns (order_id, panel_username, status) for every order that has a
#     panel account -- i.e. everything past the pending/rejected payment stage.
#     Only the columns the status-sync needs are selected to keep it light."""
#     async with SessionLocal() as session:
#         result = await session.execute(
#             select(Order.order_id, Order.panel_username, Order.status).where(
#                 Order.panel_username.is_not(None),
#                 Order.status.notin_(UNPROVISIONED_STATUSES),
#             )
#         )
#         return [tuple(row) for row in result.all()]
#
#
# async def bulk_update_order_statuses(updates: list[tuple[int, OrderStatus]]) -> int:
#     """Applies many order status changes in a single executemany UPDATE
#     (one round-trip instead of one UPDATE per order).
#
#     `updates` is a list of (order_id, new_status); returns the count applied.
#     Uses the Core table to avoid ORM identity-map sync on executemany."""
#     if not updates:
#         return 0
#
#     table = Order.__table__
#     async with SessionLocal() as session:
#         await session.execute(
#             table.update()
#             .where(table.c.order_id == bindparam("b_order_id"))
#             .values(status=bindparam("b_status")),
#             [
#                 {"b_order_id": order_id, "b_status": status}
#                 for order_id, status in updates
#             ],
#         )
#         await session.commit()
#     return len(updates)


async def create_support_message(message_id: int, from_user_id: int) -> int:
    async with SessionLocal() as session:
        support = Support(
            admin_message_id=message_id,
            from_user_id=from_user_id
        )
        session.add(support)
        await session.commit()
        await session.refresh(support)
        return support.admin_message_id


async def get_support_message_user(message_id: int) -> int:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Support.from_user_id)
            .where(Support.admin_message_id == message_id, Support.status == "pending")
        )
        return result.scalar_one_or_none()

