"""Read-only YooKassa status client. It exposes no mutation methods."""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

API_URL = "https://api.yookassa.ru/v3"
_PAYMENT_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_KNOWN_STATUSES = {"pending", "waiting_for_capture", "succeeded", "canceled"}


class YooKassaCheckError(RuntimeError):
    """Provider check failed; deliberately contains no credential or payment ID."""


class YooKassaReadOnlyClient:
    def __init__(self, shop_id: str, secret_key: str) -> None:
        self._auth = aiohttp.BasicAuth(shop_id, secret_key)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def get_payment_status(self, payment_id: str, expected_amount: int) -> dict[str, Any]:
        if not _PAYMENT_ID.fullmatch(payment_id):
            raise YooKassaCheckError("Stored payment reference is invalid")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                headers={"Accept": "application/json"},
            )
        try:
            async with self._session.get(f"{API_URL}/payments/{payment_id}") as response:
                if response.status != 200:
                    raise YooKassaCheckError("YooKassa could not confirm this payment")
                payload = await response.json(content_type=None)
        except YooKassaCheckError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise YooKassaCheckError("YooKassa is temporarily unavailable") from None

        if not isinstance(payload, dict):
            raise YooKassaCheckError("YooKassa returned an invalid payment response")
        status = payload.get("status")
        paid = payload.get("paid")
        amount_data = payload.get("amount")
        if not isinstance(status, str) or not isinstance(paid, bool) or not isinstance(amount_data, dict):
            raise YooKassaCheckError("YooKassa returned incomplete payment data")
        if status not in _KNOWN_STATUSES:
            raise YooKassaCheckError("YooKassa returned an unrecognized payment status")
        try:
            amount = Decimal(str(amount_data["value"]))
            expected = Decimal(expected_amount)
        except (KeyError, InvalidOperation, ValueError):
            raise YooKassaCheckError("Payment amount could not be verified") from None
        currency = amount_data.get("currency")
        if not amount.is_finite() or currency != "RUB":
            raise YooKassaCheckError("Payment currency or amount could not be verified")
        if amount + Decimal("0.01") < expected:
            amount_matches = False
        else:
            amount_matches = True
        timestamp = payload.get("created_at")
        return {
            "status": status[:40],
            "paid": paid,
            "amount_rub": str(amount.quantize(Decimal("0.01"))),
            "matches_order_amount": amount_matches,
            "created_at": timestamp[:64] if isinstance(timestamp, str) else None,
        }
