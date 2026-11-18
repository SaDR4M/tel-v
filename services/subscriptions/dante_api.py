"""Adapter for the DANTE VPN API used by the Unlimited plan."""
import time
from datetime import datetime, timedelta

import aiohttp

from config import settings

from .base import (
    ProvisionRequest,
    SubscriptionError,
    SubscriptionUser,
)

DANTE_STATUSES = {"active", "limited", "expired", "disabled"}
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)


class DanteAPIError(SubscriptionError):
    pass


class DanteClient:
    def __init__(self):
        self.base_url = settings.DANTE_API_BASE_URL.rstrip("/")

    def _headers(self) -> dict:
        if not settings.DANTE_API_KEY:
            raise DanteAPIError("DANTE_API_KEY is not configured")
        return {"Authorization": f"Bearer {settings.DANTE_API_KEY}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        allow_404: bool = False,
    ) -> dict | None:
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(),
                    json=json,
                    params=params,
                ) as response:
                    if response.status == 404 and allow_404:
                        return None
                    if response.status not in (200, 201):
                        body = await response.text()
                        raise DanteAPIError(
                            f"{method} {path} failed: "
                            f"{response.status} {body[:300]}"
                        )
                    return await response.json()
        except DanteAPIError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DanteAPIError(f"DANTE API unreachable: {exc}") from exc

    async def create_user(
        self,
        *,
        name: str,
        expires_date: str,
        note: str,
    ) -> dict:
        payload = {
            "name": name,
            "quota_gb": 0,
            "expires_date": expires_date,
            "note": note,
            "enabled": True,
        }
        response = await self._request("POST", "/users", json=payload)
        return (response or {}).get("data") or {}

    async def get_user(self, user_id: str) -> dict | None:
        response = await self._request(
            "GET", f"/users/{user_id}", allow_404=True
        )
        return (response or {}).get("data")

    async def extend_user(self, user_id: str, expires_date: str) -> dict:
        response = await self._request(
            "PATCH",
            f"/users/{user_id}",
            json={"expires_date": expires_date, "enabled": True},
        )
        data = (response or {}).get("data") or {}
        if data and not data.get("sub_url"):
            current = await self.get_user(user_id)
            if current:
                data = current
        return data

    async def list_users(self) -> list[dict]:
        users: list[dict] = []
        page = 1
        page_size = 100
        while True:
            response = await self._request(
                "GET",
                "/users",
                params={"page": page, "page_size": page_size},
            )
            data = (response or {}).get("data") or []
            users.extend(data)
            meta = (response or {}).get("meta") or {}
            total = int(meta.get("total") or 0)
            if len(data) < page_size or page * page_size >= total:
                break
            page += 1
        return users


def _status(user: dict) -> str:
    if user.get("enabled") is False:
        return "disabled"
    raw = (user.get("status") or "").lower()
    if raw in DANTE_STATUSES:
        return raw
    expires_at = user.get("expires_at")
    if expires_at and int(expires_at) <= int(time.time()):
        return "expired"
    return "active"


def _to_subscription(user: dict) -> SubscriptionUser:
    expires_at = user.get("expires_at")
    return SubscriptionUser(
        provider="dante_api",
        user_id=str(user.get("id") or ""),
        # The API-generated email is the stable panel identifier; `name` is
        # the buyer-requested display name shown in the panel/app.
        username=user.get("email") or user.get("name"),
        subscription_url=user.get("sub_url") or "",
        status=_status(user),
        expires_at=(
            datetime.fromtimestamp(int(expires_at)) if expires_at else None
        ),
        used_bytes=int(user.get("used_bytes") or 0),
        quota_bytes=int(user.get("quota_bytes") or 0),
        enabled=bool(user.get("enabled", True)),
    )


def _expires_date(duration_days: int) -> str:
    """DANTE expects an ISO date: YYYY-MM-DD."""
    return (datetime.now() + timedelta(days=duration_days)).strftime("%Y-%m-%d")


class DanteProvider:
    name = "dante_api"

    def __init__(self):
        self.client = DanteClient()

    def _name(self, request: ProvisionRequest) -> str:
        # DANTE generates the unique panel identifier (email) itself; `name`
        # is only the buyer-facing label shown in the app/subscription.
        return (request.requested_username or "").strip()[:64] or "user"

    async def create(self, request: ProvisionRequest) -> SubscriptionUser:
        data = await self.client.create_user(
            name=self._name(request),
            expires_date=_expires_date(int(request.plan["duration_days"])),
            note=(
                f"bot order #{request.order_id}; "
                f"telegram {request.telegram_id}"
            ),
        )
        if not data.get("id"):
            raise DanteAPIError("DANTE API returned no user id")
        return _to_subscription(data)

    async def extend(
        self, user_id: str, request: ProvisionRequest
    ) -> SubscriptionUser:
        data = await self.client.extend_user(
            user_id,
            _expires_date(int(request.plan["duration_days"])),
        )
        if not data.get("id"):
            raise DanteAPIError("DANTE API returned no user id")
        return _to_subscription(data)

    async def get(self, user_id: str) -> SubscriptionUser | None:
        data = await self.client.get_user(user_id)
        return _to_subscription(data) if data else None

    async def list_users(self) -> list[SubscriptionUser]:
        return [_to_subscription(user) for user in await self.client.list_users()]
