"""
Thin async client for a Marzban-compatible panel API (matches the
RickPanel-style JSON schema: proxies / expire / data_limit / sub_token).

If your panel's endpoints differ, this is the only file you should
need to edit.
"""
import time
from logging import getLogger
import uuid
import aiohttp
from config import settings
import enum
from typing import Optional

logger = getLogger(__name__)

class PanelAPIError(Exception):
    pass


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    LIMITED = "limited"
    EXPIRED = "expired"
    DISABLED = "disabled"


class PanelClient:
    def __init__(self):
        self._token: str | None = None
        self._token_expiry: float = 0

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        # Cache the admin token for ~55 minutes (typical panel tokens last 1hr)
        if self._token and time.time() < self._token_expiry:
            return self._token

        async with session.post(
            f"{settings.PANEL_BASE_URL}/api/admin/token",
            data={
                "username": settings.PANEL_ADMIN_USERNAME,
                "password": settings.PANEL_ADMIN_PASSWORD,
                "grant_type": "password",
            },
        ) as resp:
            if resp.status != 200:
                raise PanelAPIError(f"Auth failed: {resp.status} {await resp.text()}")
            data = await resp.json()
            self._token = data["access_token"]
            self._token_expiry = time.time() + 55 * 60
            return self._token

    async def _headers(self, session: aiohttp.ClientSession) -> dict:
        token = await self._get_token(session)
        return {"Authorization": f"Bearer {token}"}

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        """GET a JSON endpoint; network failures surface as PanelAPIError so
        callers only ever handle one exception type."""
        try:
            async with aiohttp.ClientSession() as session:
                headers = await self._headers(session)
                async with session.get(
                    f"{settings.PANEL_BASE_URL}{path}",
                    headers=headers,
                    params=params,
                ) as resp:
                    if resp.status != 200:
                        raise PanelAPIError(
                            f"GET {path} failed: {resp.status} {await resp.text()}"
                        )
                    return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PanelAPIError(f"Panel unreachable: {e}") from e

    async def get_allocation(self) -> dict:
        """How much volume this admin has handed out and how much of it is
        used (/api/admin/allocation — bytes: created/used/unlimited)."""
        return await self._get_json("/api/admin/allocation")

    async def get_system_stats(self) -> dict:
        """Panel version, user counts and traffic figures (/api/system)."""
        return await self._get_json("/api/system")

    async def get_overview(self) -> dict:
        """Online user counts and the provider subscription package
        (/api/overview — subscription.traffic_limit/traffic_used in bytes)."""
        return await self._get_json("/api/overview")

    async def create_user(
        self,
        username: str,
        telegram_id: int,
        data_limit_bytes: int,
        duration_days: int,
    ) -> dict:
        """Creates a new panel user and returns the raw user object
        (includes subscription_url)."""
        expire_ts = int(time.time()) + duration_days * 86400
        uid = str(uuid.uuid4())

        logger.error(username)
        payload = {
            "username": username,
            "proxies": {
                "vmess": {"id": uid},
                "vless": {"id": uid, "flow": "xtls-rprx-vision"},
            },
            "expire": expire_ts,
            "data_limit": data_limit_bytes,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": str(telegram_id),
        }

        async with aiohttp.ClientSession() as session:
            headers = await self._headers(session)
            async with session.post(
                f"{settings.PANEL_BASE_URL}/api/user",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status not in (200, 201):
                    raise PanelAPIError(
                        f"Create user failed: {resp.status} {await resp.text()}"
                    )
                return await resp.json()

    async def get_user(self, username: str) -> dict | None:
        async with aiohttp.ClientSession() as session:
            headers = await self._headers(session)
            async with session.get(
                f"{settings.PANEL_BASE_URL}/api/user/{username}",
                headers=headers,
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise PanelAPIError(
                        f"Get user failed: {resp.status} {await resp.text()}"
                    )
                return await resp.json()

    async def modify_user(self, username: str, **fields) -> dict:
        async with aiohttp.ClientSession() as session:
            headers = await self._headers(session)
            async with session.put(
                f"{settings.PANEL_BASE_URL}/api/user/{username}",
                json=fields,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    raise PanelAPIError(
                        f"Modify user failed: {resp.status} {await resp.text()}"
                    )
                return await resp.json()


    async def reset_user_usage(self, username: str) -> dict:
        """Zeroes out a user's used_traffic counter without touching their
        proxies/link. Marzban-style panels expose this as its own endpoint
        rather than as a field on the modify_user payload."""
        async with aiohttp.ClientSession() as session:
            headers = await self._headers(session)
            async with session.post(
                f"{settings.PANEL_BASE_URL}/api/user/{username}/reset",
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    raise PanelAPIError(
                        f"Reset usage failed: {resp.status} {await resp.text()}"
                    )
                return await resp.json()

    async def renew_user(self, username: str, data_limit_bytes: int, duration_days: int) -> dict:
            """Extends an EXISTING user's expiry/data and resets their usage
            counter, without regenerating proxies/UUIDs -- so the subscription
            link the user already has keeps working unchanged."""
            expire_ts = int(time.time()) + duration_days * 86400
            await self.modify_user(
                username,
                expire=expire_ts,
                data_limit=data_limit_bytes,
                status="active",
            )
            await self.reset_user_usage(username)
            # Re-fetch so the returned object reflects the reset usage too.
            return await self.get_user(username)

    async def get_users_list(
        self,
        status: Optional[UserStatus] = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Return every panel user (fully paginated), optionally filtered by
        status. Marzban-style panels cap `/api/users` at one page, so we walk
        `offset`/`limit` until all `total` rows are read -- otherwise the
        status-sync would silently miss users once the count exceeds a page.
        """
        status_value = status.value if isinstance(status, UserStatus) else status
        users: list[dict] = []
        offset = 0

        async with aiohttp.ClientSession() as session:
            headers = await self._headers(session)
            while True:
                params = {"offset": offset, "limit": page_size}
                if status_value is not None:
                    params["status"] = status_value

                async with session.get(
                    f"{settings.PANEL_BASE_URL}/api/users",
                    headers=headers,
                    params=params,
                ) as resp:
                    if resp.status != 200:
                        raise PanelAPIError(
                            f"Failed to fetch list of users: {resp.status} {await resp.text()}"
                        )
                    data = await resp.json()

                batch = data.get("users") or []
                users.extend(batch)
                total = data.get("total")

                if len(batch) < page_size:
                    break
                offset += page_size
                if total is None or offset >= total:
                    break

        return users


panel = PanelClient()
