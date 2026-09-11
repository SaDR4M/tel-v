from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_username: Mapped[str | None] = mapped_column(String, nullable=True)
    panel_username: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(Integer)
    plan_id: Mapped[str] = mapped_column(String)
    requested_username: Mapped[str | None] = mapped_column(String, nullable=True)
    order_type: Mapped[str] = mapped_column(String, default="purchase")  # purchase / renewal
    target_panel_username: Mapped[str | None] = mapped_column(String, nullable=True)  # set when order_type == "renewal"
    status: Mapped[str] = mapped_column(String, default="pending")  # pending / approved / rejected
    receipt_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    admin_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    panel_username: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())