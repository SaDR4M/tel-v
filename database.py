from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config import settings
from models import Base, User, Order, Support, NotificationLog, NotificationKind, NotificationStatus
from datetime import datetime

engine = create_async_engine(f"sqlite+aiosqlite:///{settings.DB_PATH}")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
    """Returns this user's approved orders (i.e. subscriptions that were
    actually created on the panel), most recent first."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order)
            .where(Order.telegram_id == telegram_id, Order.status == "approved", Order.expire_at >= datetime.now())
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
            .where(Order.telegram_id == telegram_id, Order.status == "approved")
            .order_by(Order.created_at.desc())
        )
        rows = result.all()
        seen: dict[str, None] = {}
        for panel_username, target_panel_username in rows:
            for candidate in (panel_username, target_panel_username):
                if candidate and candidate not in seen:
                    seen[candidate] = None
        return list(seen.keys())


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

