"""HTTP-сервер для приёма уведомлений ЮKassa (webhook).

ЮKassa присылает POST с телом вида::

    {"type": "notification", "event": "payment.succeeded", "object": {...}}

Телу уведомления доверять нельзя: получив событие, бот переспрашивает платёж
через API (``GET /v3/payments/{id}``) и выдаёт доступ, только если платёж
действительно ``succeeded`` и сумма совпадает с суммой заказа. Если выдача
не удалась, отдаём 500, а если недоступна сама ЮKassa — 503; в обоих случаях
ЮKassa повторит уведомление.
"""

from __future__ import annotations

import ipaddress
import json
import logging

from aiogram import Bot
from aiohttp import web

from app.config import Config
from app.db import Database
from app.logctx import log_context
from app.services.payments import PaymentError, YooKassaClient
from app.services.subs import complete_order

log = logging.getLogger(__name__)

# Диапазоны IP, с которых ЮKassa отправляет уведомления (по документации).
YOOKASSA_NETS = [
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.154.128/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "2a02:5180::/32",
]


def _ip_allowed(remote: str) -> bool:
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    for cidr in YOOKASSA_NETS:
        if address in ipaddress.ip_network(cidr):
            return True
    return False


def _client_ip(request: web.Request, trust_proxy: bool) -> str:
    """IP источника запроса.

    По умолчанию берётся реальный TCP-адрес соединения. Заголовок
    X-Forwarded-For учитывается только если WEBHOOK_TRUST_PROXY=true —
    иначе любой клиент мог бы подставить IP ЮKassa и обойти фильтр.
    """
    if trust_proxy:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote or ""


async def start_webhook_server(cfg: Config, db: Database, bot: Bot, yk: YooKassaClient | None):
    """Поднимает aiohttp-сервер с маршрутом уведомлений. Возвращает AppRunner."""

    async def handle_yookassa(request: web.Request) -> web.Response:
        # Один request_id на уведомление — связывает все его логи.
        with log_context():
            return await _process_yookassa(request)

    async def _process_yookassa(request: web.Request) -> web.Response:
        remote = _client_ip(request, cfg.webhook_trust_proxy)
        if not _ip_allowed(remote):
            log.warning("Вебхук ЮKassa отклонён: IP %s вне разрешённых диапазонов", remote)
            return web.Response(status=403, text="forbidden")

        raw = await request.text()
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            log.warning("Некорректный JSON от ЮKassa: %s", raw[:200])
            return web.Response(status=400, text="bad json")

        event = payload.get("event", "")
        obj = payload.get("object") or {}
        payment_id = obj.get("id", "")
        log.info("Вебхук ЮKassa: %s, платёж %s", event, payment_id)

        if event != "payment.succeeded" or not payment_id:
            # Прочие события просто подтверждаем.
            return web.Response(status=200, text="ignored")

        order = await db.fetchone(
            "SELECT * FROM orders WHERE payment_id = ? ORDER BY id DESC LIMIT 1", (payment_id,)
        )
        if order is None:
            log.warning("Платёж %s не привязан ни к одному заказу", payment_id)
            return web.Response(status=200, text="unknown payment")

        if order["status"] == "paid":
            return web.Response(status=200, text="already paid")

        if yk is None:
            return web.Response(status=500, text="yookassa disabled")

        # Сетевой сбой (ЮKassa недоступна из VPS) — это НЕ «не оплачено»:
        # отдаём 503, чтобы ЮKassa повторила уведомление, а не считала его
        # доставленным. Иначе платёж «зависает» до опроса раз в 3 минуты.
        try:
            payment = await yk.get_payment(payment_id)
        except PaymentError as exc:
            log.warning("ЮKassa недоступна при проверке %s: %s — просим повтор", payment_id, exc)
            return web.Response(status=503, text="yookassa unavailable, retry")

        if not yk.payment_is_valid(payment, int(order["amount"])):
            log.warning("Платёж %s не подтверждён через API — пропускаем", payment_id)
            return web.Response(status=200, text="not verified")

        try:
            await complete_order(bot, cfg, db, int(order["id"]), payment_id=payment_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка выдачи по платежу %s: %s", payment_id, exc)
            return web.Response(status=500, text="retry later")

        return web.Response(status=200, text="ok")

    async def handle_health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "opvpn webhook"})

    app = web.Application()
    app.router.add_post(cfg.webhook_path, handle_yookassa)
    app.router.add_get("/healthz", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port)
    await site.start()
    log.info(
        "Вебхуки ЮKassa слушаем на %s:%s%s (публичный URL: %s)",
        cfg.webhook_host, cfg.webhook_port, cfg.webhook_path, cfg.webhook_public_url or "не задан",
    )
    return runner
