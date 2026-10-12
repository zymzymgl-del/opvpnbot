"""Платежи: ЮKassa (карты/СБП), ЮMoney и Telegram Stars."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

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
                             payment_method: str | None = None) -> dict[str, Any]:
        """Создаёт платёж и возвращает объект ЮKassa (нужен confirmation_url)."""
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self.return_url},
            "description": description[:128],
            "metadata": {k: str(v) for k, v in metadata.items()},
        }
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

        Чистая функция без сети — годится и для вебхука, и для опроса.
        """
        if payment.get("status") != "succeeded" or not payment.get("paid", False):
            return False
        if expected_amount is not None:
            try:
                value = float(payment["amount"]["value"])
            except (KeyError, TypeError, ValueError):
                return False
            if abs(value - expected_amount) > 0.01:
                log.warning("Сумма платежа %s (%s) не совпала с ожидаемой (%s)",
                            payment.get("id", "?"), value, expected_amount)
                return False
        return True

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


async def create_stars_invoice(bot, *, chat_id: int, order_id: int, title: str, description: str,
                               stars_amount: int, payload: str | None = None) -> None:
    """Отправляет счёт в Telegram Stars (валюта XTR, provider_token не нужен)."""
    from aiogram.types import LabeledPrice

    await bot.send_invoice(
        chat_id=chat_id,
        title=title[:32],
        description=description[:255],
        payload=payload or f"order:{order_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="VPN", amount=stars_amount)],
    )


async def refund_stars(bot, *, user_id: int, charge_id: str) -> bool:
    """Возврат звёзд (например, при отмене заказа)."""
    try:
        return bool(await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id))
    except Exception as exc:  # pragma: no cover
        log.warning("Не удалось вернуть звёзды: %s", exc)
        return False


class YooMoneyClient:
    """Приём платежей на кошелёк ЮMoney: ссылка на форму + автопроверка по label.

    Пользователь платит по ссылке quickpay (картой или с кошелька), в перевод
    подставляется label заказа; бот находит входящую операцию в истории
    (operation-history) и подтверждает заказ. Вебхуки и домен не нужны.
    """

    API_URL = "https://yoomoney.ru/api"
    FORM_URL = "https://yoomoney.ru/quickpay/confirm.xml"

    def __init__(self, cfg: Config) -> None:
        self.wallet = cfg.yoomoney_wallet
        self.token = cfg.yoomoney_token
        self.bot_name = cfg.bot_name
        self.proxy = cfg.payments_proxy or None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @staticmethod
    def label_for(order_id: int) -> str:
        return f"opvpnbot-order-{order_id}"

    def form_url(self, amount_rub: int, order_id: int) -> str:
        """Ссылка на платёжную форму ЮMoney с суммой и меткой заказа."""
        params = {
            "receiver": self.wallet,
            "quickpay-form": "shop",
            "targets": f"Оплата заказа {self.bot_name} #{order_id}",
            "paymentType": "AC",
            "sum": f"{amount_rub:.2f}",
            "label": self.label_for(order_id),
        }
        return f"{self.FORM_URL}?" + urlencode(params)

    async def find_payment(self, order_id: int, amount_rub: int) -> str | None:
        """Ищет входящий успешный перевод с label заказа и нужной суммой.

        Возвращает operation_id или None.
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.API_URL}/operation-history",
                params={"label": self.label_for(order_id), "details": "true"},
                proxy=self.proxy,
            ) as resp:
                if resp.status != 200:
                    log.warning("ЮMoney operation-history: HTTP %s", resp.status)
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("ЮMoney: проверка не удалась: %s", exc)
            return None
        for op in (data.get("operations") or []):
            if op.get("direction") != "in" or op.get("status") != "success":
                continue
            if op.get("label") != self.label_for(order_id):
                continue
            try:
                got = float(op.get("amount", 0))
            except (TypeError, ValueError):
                continue
            if abs(got - amount_rub) <= 0.01:
                return str(op.get("operation_id") or op.get("label"))
        return None
