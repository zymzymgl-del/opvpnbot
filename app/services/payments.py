"""Платежи: ЮKassa (карты РФ, СБП)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

from app.config import Config

log = logging.getLogger(__name__)

YOOKASSA_API = "https://api.yookassa.ru/v3"


class PaymentError(RuntimeError):
    pass


class YooKassaClient:
    """Минимальный клиент ЮKassa REST API (создание платежа и проверка статуса)."""

    def __init__(self, cfg: Config) -> None:
        self.shop_id = cfg.yookassa_shop_id
        self.secret_key = cfg.yookassa_secret_key
        self.return_url = cfg.yookassa_return_url
        self.proxy = cfg.payments_proxy or None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            auth = aiohttp.BasicAuth(self.shop_id, self.secret_key)
            # api.yookassa.ru отдаёт НЕСКОЛЬКО A-записей; часть из них может
            # быть недоступна с конкретного VPS. happy-eyeballs (включён явно,
            # чтобы не зависеть от версии aiohttp) поднимает соединение со
            # всеми адресами параллельно, а короткие connect/total не дают
            # мёртвому адресу «съесть» весь бюджет запроса: иначе вебхук
            # отдаёт 504, а проверка падает с asyncio.TimeoutError.
            connector = aiohttp.TCPConnector(
                happy_eyeballs_delay=0.25,
                interleave=1,
            )
            self._session = aiohttp.ClientSession(
                auth=auth,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def create_payment(self, *, amount_rub: int, description: str, metadata: dict[str, Any],
                             idempotence_key: str | None = None,
                             payment_method: str | None = None,
                             ttl_seconds: int | None = None) -> dict[str, Any]:
        """Создаёт платёж и возвращает объект ЮKassa (нужен confirmation_url).

        ``idempotence_key`` — ключ идемпотентности ЮKassa: повторный запрос с тем
        же ключом возвращает уже созданный платёж, а не создаёт новый.
        ``ttl_seconds`` — сколько времени счёт остаётся оплачиваемым.
        """
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self.return_url},
            "description": description[:128],
            "metadata": {k: str(v) for k, v in metadata.items()},
        }
        if ttl_seconds:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            payload["expires_at"] = expires_at.isoformat(timespec="seconds").replace("+00:00", "Z")
        if payment_method == "sbp":
            payload["payment_method_data"] = {"type": "sbp"}
        session = await self._get_session()
        headers = {"Idempotence-Key": idempotence_key or str(uuid.uuid4())}
        try:
            async with session.post(f"{YOOKASSA_API}/payments", json=payload, headers=headers,
                                    proxy=self.proxy) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    raise PaymentError(f"ЮKassa вернула HTTP {resp.status}: {raw[:300]}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Сеть/DNS/SSL недоступны — это не ошибка API, но вызывающий код
            # (и пользователь) должны получить понятный PaymentError.
            raise PaymentError(f"Нет связи с ЮKassa ({type(exc).__name__}): {exc}") from exc

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(f"{YOOKASSA_API}/payments/{payment_id}",
                                   proxy=self.proxy) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    raise PaymentError(f"ЮKassa вернула HTTP {resp.status}: {raw[:300]}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PaymentError(f"Нет связи с ЮKassa ({type(exc).__name__}): {exc}") from exc

    @staticmethod
    def payment_is_valid(payment: dict[str, Any], expected_amount: int | None = None) -> bool:
        """Проверяет объект платежа ЮKassa: ``succeeded`` + ``paid`` и (если задано) сумму.

        Сумма сверяется по правилу «не меньше ожидаемой»: платёж, выставленный на
        большую сумму (например, промокод применили уже после выставления счёта),
        тоже считается валидным — переплату принимаем, чтобы не потерять оплату.
        Меньшая сумма — отказ (защита от недоплаты).
        ``expected_amount`` — в рублях.

        Чистая функция без сети — годится и для вебхука, и для опроса.
        """
        if payment.get("status") != "succeeded" or not payment.get("paid", False):
            return False
        if expected_amount is not None:
            try:
                value = float(payment["amount"]["value"])
            except (KeyError, TypeError, ValueError):
                return False
            if value + 0.01 < expected_amount:
                log.warning("Сумма платежа %s (%s) МЕНЬШЕ ожидаемой (%s) — платёж не зачислен",
                            payment.get("id", "?"), value, expected_amount)
                return False
            if value > expected_amount + 0.01:
                log.info("Платёж %s оплачен с переплатой: %s вместо %s",
                         payment.get("id", "?"), value, expected_amount)
        return True

    @staticmethod
    def payment_is_done(payment: dict[str, Any]) -> bool:
        """Проведённый платёж: ``succeeded`` (или уже ``canceled``/``refunded``).

        Такой платёж нельзя «перевыпустить»: счёт считается закрытым, и заказу
        нужно выставить новый.
        """
        return payment.get("status") in ("succeeded", "canceled", "refunded")

    @staticmethod
    def payment_is_payable(payment: dict[str, Any]) -> bool:
        """Платёж ещё можно оплатить (``waiting_for_capture`` / ``pending``)."""
        return payment.get("status") in ("waiting_for_capture", "pending")

    async def is_paid(self, payment_id: str, expected_amount: int | None = None) -> bool:
        """Проверяет платёж напрямую в API — единственный надёжный способ
        подтвердить уведомление/оплату (телу вебхука доверять нельзя).

        Возвращает ``False`` и при сетевом сбое (исключение не роняем) —
        вызывающий код сам решает, ответить 200 или повторить проверку позже.
        """
        try:
            payment = await self.get_payment(payment_id)
        except PaymentError as exc:
            log.warning("Проверка платежа %s не удалась: %s", payment_id, exc)
            return False
        return self.payment_is_valid(payment, expected_amount)
