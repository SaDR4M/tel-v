from sqlalchemy import select, update, delete, func, bindparam
from sqlalchemy.exc import OperationalError
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


def init_db_sync():
    """Migrate the DB without the async driver.

    Celery's ``worker_init`` runs in the parent process *before* the prefork
    pool forks. Using the async engine there starts an aiosqlite thread that
    does not survive fork, so the pool children hang forever on their first
    DB query. A short-lived synchronous engine avoids creating any threads.
    """
    from sqlalchemy import create_engine

    sync_engine = create_engine(
        f"sqlite:///{settings.DB_PATH}",
        connect_args={"timeout": 30},
    )
    try:
        with sync_engine.begin() as conn:
            Base.metadata.create_all(conn)
            _ensure_order_columns(conn)
    finally:
        sync_engine.dispose()


def _ensure_order_columns(conn):
    """Additively patch columns added after the first deploy.

    create_all never alters existing tables, so without this an upgraded
    database would lack the new columns and every ORM read would fail.
    """
    existing = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(orders)").fetchall()
    }
    additions = {
        "coupon_code": "ALTER TABLE orders ADD COLUMN coupon_code VARCHAR",
        "final_price": "ALTER TABLE orders ADD COLUMN final_price INTEGER",
        "manual_link": "ALTER TABLE orders ADD COLUMN manual_link VARCHAR",
        "telegram_username": "ALTER TABLE orders ADD COLUMN telegram_username VARCHAR",
        "superseded_at": "ALTER TABLE orders ADD COLUMN superseded_at DATETIME",
    }
    for column, statement in additions.items():
        if column in existing:
            continue
        try:
            conn.exec_driver_sql(statement)
        except OperationalError as exc:
            # The bot and the Celery worker both migrate on startup. If they
            # race, the loser finds the column already there -- that's success.
            if "duplicate column" not in str(exc).lower():
                raise


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
    telegram_username: str | None = None,
    order_type: str = "purchase",
    target_panel_username: str | None = None,
    coupon_code: str | None = None,
    final_price: int | None = None,
) -> int:
    async with SessionLocal() as session:
        order = Order(
            telegram_id=telegram_id,
            telegram_username=(telegram_username or "").strip() or None,
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


async def delete_test_orders(telegram_id: int):
    """Erase every order of a test shopper — used by automated tests that
    write throwaway rows into the real dev database."""
    async with SessionLocal() as session:
        order_ids = (
            await session.execute(
                select(Order.order_id).where(Order.telegram_id == telegram_id)
            )
        ).scalars().all()
        if order_ids:
            await session.execute(
                delete(NotificationLog).where(
                    NotificationLog.order_id.in_(order_ids)
                )
            )
        await session.execute(delete(Order).where(Order.telegram_id == telegram_id))
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


async def supersede_orders_for_panel(panel_username: str, keep_order_id: int) -> int:
    """Mark older orders for the same panel account as superseded by a renewal.

    A renewal reuses the panel username, so the previous order row keeps its old
    expiry; without this marker the expiry/usage scans would keep reminding the
    buyer about a subscription that has already been extended. Only the order
    created by the renewal stays current.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            update(Order)
            .where(
                Order.panel_username == panel_username,
                Order.order_id != keep_order_id,
                Order.superseded_at.is_(None),
            )
            .values(superseded_at=datetime.now())
        )
        await session.commit()
        return result.rowcount


async def approve_renewal_order(
    order_id: int,
    panel_username: str,
    expire_at: datetime,
):
    """Approve a renewal and retire the previous order in one transaction.

    Splitting these writes left a window where the new order was approved but
    the old order was not yet superseded, so a reminder scan could still
    message the buyer about the subscription they had just renewed.
    """
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(Order)
                .where(
                    Order.panel_username == panel_username,
                    Order.order_id != order_id,
                    Order.superseded_at.is_(None),
                )
                .values(superseded_at=datetime.now())
            )
            order = await session.get(Order, order_id)
            if order is None:
                raise ValueError(f"Order {order_id} not found")
            order.status = "approved"
            order.panel_username = panel_username
            order.expire_at = expire_at


async def set_order_expire_at(order_id: int, expire_at: datetime):
    """Fix up the expire date after approval — create_order stamps a generic
    29-day window, but the approved plan may state a different duration."""
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order:
            order.expire_at = expire_at
            await session.commit()


async def set_manual_link(order_id: int, manual_link: str):
    """Store the subscription URL the admin delivered for a manual-tier
    order; mysub shows it instead of a live panel account."""
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order:
            order.manual_link = manual_link
            await session.commit()


async def get_order_by_admin_message(admin_message_id: int) -> dict | None:
    """Order whose receipt was posted into the admin group as this message —
    used to route admin replies (manual link delivery) to the right order."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order).where(Order.admin_message_id == admin_message_id).limit(1)
        )
        order = result.scalar_one_or_none()
        if not order:
            return None
        return {c.name: getattr(order, c.name) for c in order.__table__.columns}


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
                # A renewal replaces the previous order row for the same panel
                # account; keep the stale duplicate out of mysub/the renewal menu.
                Order.superseded_at.is_(None),
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
                func.coalesce(Order.telegram_username, User.telegram_username).label("telegram_username"),
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


async def get_display_names(telegram_id: int) -> dict[str, str]:
    """panel_username -> the user-facing nickname the buyer picked when
    ordering (requested_username of the purchase order that created the
    account). Accounts without one — or a legacy/manual account — get no
    entry and callers fall back to the raw panel username. Renewal orders
    are excluded: they store target_panel_username as requested_username,
    which is not a nickname."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order.panel_username, Order.requested_username)
            .where(
                Order.telegram_id == telegram_id,
                Order.order_type == "purchase",
                Order.panel_username.isnot(None),
                Order.status.notin_(UNPROVISIONED_STATUSES),
            )
        )
        names: dict[str, str] = {}
        for panel_username, requested in result.all():
            if panel_username and requested:
                names.setdefault(panel_username, requested)
        return names


async def get_all_panel_usernames() -> list[str]:
    """Every panel username attached to any provisioned order (all buyers,
    unlike get_distinct_panel_usernames which is per-user) — used by the
    admin panel-vs-orders reconciliation report."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Order.panel_username, Order.target_panel_username)
            .where(Order.status.notin_(UNPROVISIONED_STATUSES))
        )
        return sorted(
            {n for panel_username, target in result.all()
             for n in (panel_username, target) if n}
        )


async def get_all_orders_for_admin() -> list[dict]:
    """Every order regardless of status, with lifecycle fields (status,
    order_type) for the admin analytics reports — the funnel needs pending
    and rejected orders too, which get_sales_data deliberately filters out."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(
                Order.order_id,
                Order.telegram_id,
                func.coalesce(Order.telegram_username, User.telegram_username).label("telegram_username"),
                Order.plan_id,
                Order.status,
                Order.order_type,
                Order.coupon_code,
                Order.final_price,
                Order.created_at,
            )
            .outerjoin(User, User.telegram_id == Order.telegram_id)
            .order_by(Order.created_at.asc())
        )
        return [
            {
                "order_id": order_id,
                "telegram_id": telegram_id,
                "telegram_username": telegram_username,
                "plan_id": plan_id,
                "status": status,
                "order_type": order_type,
                "coupon_code": coupon_code,
                "final_price": final_price,
                "created_at": created_at,
            }
            for order_id, telegram_id, telegram_username, plan_id, status, order_type, coupon_code, final_price, created_at in result.all()
        ]


async def count_orders() -> int:
    async with SessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(Order))
        return int(result.scalar_one())


async def get_orders_page(offset: int, limit: int) -> list[dict]:
    """One newest-first page of every order (any status) with the fields the
    admin dashboard renders: buyer identity, plan pricing/coupon, chosen name,
    delivery (panel account or manual link) and lifecycle dates."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(
                Order.order_id,
                Order.telegram_id,
                func.coalesce(Order.telegram_username, User.telegram_username).label("telegram_username"),
                Order.plan_id,
                Order.requested_username,
                Order.order_type,
                Order.status,
                Order.panel_username,
                Order.target_panel_username,
                Order.manual_link,
                Order.coupon_code,
                Order.final_price,
                Order.created_at,
                Order.expire_at,
            )
            .outerjoin(User, User.telegram_id == Order.telegram_id)
            .order_by(Order.order_id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [dict(row._mapping) for row in result.all()]


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

