"""Strictly read-only access to the OPVPN SQLite database.

Only account-scoped order fields are selected. This adapter never imports or
invokes the main bot's write-capable services.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite


class OPVPNReadOnlyStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if not self._database_path.is_file():
            raise FileNotFoundError(f"OPVPN database not found: {self._database_path}")
        uri = f"{self._database_path.resolve().as_uri()}?mode=ro"
        self._connection = await aiosqlite.connect(uri, uri=True)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA query_only=ON")
        async with self._connection.execute("PRAGMA table_info(orders)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        required = {
            "id",
            "user_id",
            "plan_code",
            "amount",
            "method",
            "status",
            "created_at",
            "paid_at",
            "grant_error",
        }
        if not required.issubset(columns):
            await self.close()
            raise RuntimeError("OPVPN orders table is missing required columns")
        async with self._connection.execute("PRAGMA table_info(subscriptions)") as cursor:
            subscription_columns = {row["name"] for row in await cursor.fetchall()}
        required_subscription_columns = {
            "id", "user_id", "plan_code", "vpn_key", "devices", "traffic_gb", "expires_at", "status"
        }
        if not required_subscription_columns.issubset(subscription_columns):
            await self.close()
            raise RuntimeError("OPVPN subscriptions table is missing required columns")

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def find_user_orders(self, telegram_id: int, limit: int = 5) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT id, plan_code, amount, method, status, created_at, paid_at "
            "FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_payment_status(self, telegram_id: int, order_id: int) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT id, amount, method, status, created_at, paid_at, "
            "CASE WHEN grant_error <> '' THEN 1 ELSE 0 END AS provisioning_issue "
            "FROM orders WHERE id = ? AND user_id = ?",
            (order_id, telegram_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def get_order_payment_check_data(
        self,
        telegram_id: int,
        order_id: int,
    ) -> dict[str, Any] | None:
        """Internal-only lookup including payment IDs; never return this object to the model."""
        connection = self._require_connection()
        async with connection.execute(
            "SELECT id, amount, method, status, created_at, paid_at, "
            "CASE WHEN grant_error <> '' THEN 1 ELSE 0 END AS provisioning_issue, "
            "payment_id, payment_ids "
            "FROM orders WHERE id = ? AND user_id = ?",
            (order_id, telegram_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        ids = [value for value in str(data.get("payment_ids") or "").split(",") if value]
        current = str(data.get("payment_id") or "")
        if current:
            ids.append(current)
        data["payment_ids_internal"] = tuple(dict.fromkeys(ids))
        data.pop("payment_id", None)
        data.pop("payment_ids", None)
        return data

    async def get_subscription_status(self, telegram_id: int) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with connection.execute(
            "SELECT id, plan_code, "
            "CASE WHEN status = 'active' AND datetime(expires_at) <= datetime('now') "
            "THEN 'expired' ELSE status END AS status, "
            "expires_at, devices, traffic_gb "
            "FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (telegram_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def check_key_record(self, telegram_id: int, subscription_id: int) -> dict[str, Any] | None:
        """Check only whether a local key value exists; never select the value itself."""
        connection = self._require_connection()
        async with connection.execute(
            "SELECT id, plan_code, "
            "CASE WHEN status = 'active' AND datetime(expires_at) <= datetime('now') "
            "THEN 'expired' ELSE status END AS status, expires_at, "
            "CASE WHEN vpn_key IS NOT NULL AND vpn_key <> '' THEN 1 ELSE 0 END AS key_record_present "
            "FROM subscriptions WHERE id = ? AND user_id = ?",
            (subscription_id, telegram_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("OPVPN read-only store is not connected")
        return self._connection
