"""Client for a narrowly scoped internal OPVPN read-only status endpoint.

This client intentionally does not connect to 3x-ui. The 3x-ui API token is a
full-admin credential, so panel access must stay inside the main OPVPN process.
"""

from __future__ import annotations

from typing import Any

import aiohttp


ALLOWED_STATUSES = frozenset({"enabled", "disabled", "expired", "not_found", "unknown"})


class OPVPNPanelStatusClient:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 8.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def check_subscription(self, telegram_id: int, subscription_id: int) -> dict[str, Any]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        url = (
            f"{self._base_url}/internal/support/subscriptions/"
            f"{subscription_id}/panel-status"
        )
        async with self._session.get(
            url,
            params={"telegram_id": str(telegram_id)},
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        ) as response:
            if response.status != 200:
                # Do not put response bodies into exceptions or logs: they may contain secrets.
                raise RuntimeError(f"OPVPN panel status endpoint returned HTTP {response.status}")
            payload = await response.json(content_type=None)

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("OPVPN panel status endpoint returned an invalid response")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("OPVPN panel status endpoint omitted data")
        status = data.get("status")
        reachable = data.get("panel_reachable")
        if status not in ALLOWED_STATUSES or not isinstance(reachable, bool):
            raise RuntimeError("OPVPN panel status endpoint returned an unsupported status")
        result: dict[str, Any] = {
            "status": status,
            "panel_reachable": reachable,
        }
        expires_at = data.get("expires_at")
        if isinstance(expires_at, str) and len(expires_at) <= 64:
            result["expires_at"] = expires_at
        return result
