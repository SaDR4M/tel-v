"""Adapter for the existing Marzban-compatible VIP panel."""
from datetime import datetime

from models import PANEL_STATUS_TO_ORDER_STATUS
from panel_api import PanelAPIError, panel
from utils import make_panel_username

from .base import ProvisionRequest, SubscriptionError, SubscriptionUser


def _to_subscription(user: dict) -> SubscriptionUser:
    raw_status = (user.get("status") or "").lower()
    mapped = PANEL_STATUS_TO_ORDER_STATUS.get(raw_status)
    status = mapped.value if mapped else (raw_status or "active")
    expire = user.get("expire")
    expires_at = (
        datetime.fromtimestamp(float(expire)) if expire else None
    )
    return SubscriptionUser(
        provider="vip_panel",
        user_id=str(user.get("username") or ""),
        username=user.get("username"),
        subscription_url=user.get("subscription_url") or "",
        status=status,
        expires_at=expires_at,
        used_bytes=int(user.get("used_traffic") or 0),
        quota_bytes=int(user.get("data_limit") or 0),
        enabled=status != "disabled",
    )


class VipPanelProvider:
    name = "vip_panel"

    async def _free_username(self, base: str) -> str:
        candidate = base
        n = 1
        while await panel.get_user(candidate):
            n += 1
            candidate = f"{base}_{n}"
            if n > 20:
                raise SubscriptionError(
                    f"can't find a free username variant for {base!r}"
                )
        return candidate

    async def create(self, request: ProvisionRequest) -> SubscriptionUser:
        username = await self._free_username(
            make_panel_username(request.requested_username, request.telegram_id)
        )
        try:
            user = await panel.create_user(
                username=username,
                telegram_id=request.telegram_id,
                data_limit_bytes=int(request.plan["data_limit_bytes"]),
                duration_days=int(request.plan["duration_days"]),
            )
        except PanelAPIError as exc:
            raise SubscriptionError(str(exc)) from exc
        return _to_subscription(user)

    async def extend(
        self, user_id: str, request: ProvisionRequest
    ) -> SubscriptionUser:
        try:
            user = await panel.renew_user(
                username=user_id,
                data_limit_bytes=int(request.plan["data_limit_bytes"]),
                duration_days=int(request.plan["duration_days"]),
            )
        except PanelAPIError as exc:
            raise SubscriptionError(str(exc)) from exc
        return _to_subscription(user)

    async def get(self, user_id: str) -> SubscriptionUser | None:
        try:
            user = await panel.get_user(user_id)
        except PanelAPIError as exc:
            raise SubscriptionError(str(exc)) from exc
        return _to_subscription(user) if user else None

    async def list_users(self) -> list[SubscriptionUser]:
        try:
            users = await panel.get_users_list(status=None)
        except PanelAPIError as exc:
            raise SubscriptionError(str(exc)) from exc
        return [_to_subscription(user) for user in users]
