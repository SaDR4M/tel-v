"""
Thin async client for a Marzban-compatible panel API (matches the
RickPanel-style JSON schema: proxies / expire / data_limit / sub_token).

If your panel's endpoints differ, this is the only file you should
need to edit.
"""
import time
import uuid
import aiohttp
from config import settings


class PanelAPIError(Exception):
    pass


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


panel = PanelClient()
