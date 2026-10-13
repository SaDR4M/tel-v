from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import ForeignKey, Enum, UniqueConstraint
import enum


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_username: Mapped[str | None] = mapped_column(String, nullable=True)
    panel_username: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    LIMITED = "limited"
    EXPIRED = "expired"


# Orders that have not (yet) resulted in a panel account. Used by the
# payment-oriented queries so orders do not drop out once the status sync
# flips them to a panel status (approved -> limited/expired).
UNPROVISIONED_STATUSES = (OrderStatus.PENDING,)


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(Integer)
    plan_id: Mapped[str] = mapped_column(String)
    requested_username: Mapped[str | None] = mapped_column(String, nullable=True)
    order_type: Mapped[str] = mapped_column(String, default="purchase")  # purchase / renewal
    target_panel_username: Mapped[str | None] = mapped_column(String, nullable=True)  # set when order_type == "renewal"
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            native_enum=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=OrderStatus.PENDING
    )
    receipt_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    admin_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    panel_username: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expire_at: Mapped[datetime] = mapped_column(DateTime)


class Support(Base):
    __tablename__ = "supports"

    admin_message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_user_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="pending")


class NotificationKind(str, enum.Enum):
    USAGE_90 = "usage_90"
    EXPIRY_2D = "expiry_2d"


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"   # claimed, send not confirmed yet
    SENT = "sent"
    FAILED = "failed"     # permanent failure (blocked bot, chat not found)


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[NotificationKind] = mapped_column(
        Enum(NotificationKind, native_enum=False, length=20), nullable=False
    )
    cycle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, native_enum=False, length=20),
        nullable=False,
        default=NotificationStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "order_id", "kind", "cycle_expires_at",
            name="uq_notification_per_cycle",
        ),
    )
