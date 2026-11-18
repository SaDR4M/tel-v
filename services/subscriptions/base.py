"""Provider-neutral subscription types.

Handlers and Celery tasks depend on these types, never on a concrete panel
client. Adding or replacing a provider should only require a new adapter and
a registry entry.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class SubscriptionError(Exception):
    """Any provider failure that should be shown to the admin/user."""


@dataclass(frozen=True)
class SubscriptionUser:
    provider: str
    user_id: str
    username: str | None
    subscription_url: str
    status: str
    expires_at: datetime | None = None
    used_bytes: int = 0
    quota_bytes: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class ProvisionRequest:
    order_id: int
    telegram_id: int
    requested_username: str
    plan: dict


class SubscriptionProvider(Protocol):
    name: str

    async def create(self, request: ProvisionRequest) -> SubscriptionUser:
        ...

    async def extend(
        self, user_id: str, request: ProvisionRequest
    ) -> SubscriptionUser:
        ...

    async def get(self, user_id: str) -> SubscriptionUser | None:
        ...

    async def list_users(self) -> list[SubscriptionUser]:
        ...
