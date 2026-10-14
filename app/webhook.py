"""HTTP-сервер вебхуков: уведомления ЮKassa и апдейты Telegram.

ЮKassa присылает POST с телом вида::

    {"type": "notification", "event": "payment.succeeded", "object": {...}}

Телу уведомления доверять нельзя: получив событие, бот переспрашивает платёж
через API (``GET /v3/payments/{id}``) и выдаёт доступ, только если платёж
действительно ``succeeded`` и сумма совпадает с суммой заказа. Если выдача
не удалась, отдаём 500, а если недоступна сама ЮKassa — 503; в обоих случаях
ЮKassa повторит уведомление.

Telegram (``TG_WEBHOOK_ENABLED=true``) присылает апдейты на ``TG_WEBHOOK_URL``.
Подлинность запроса проверяется по заголовку ``X-Telegram-Bot-Api-Secret-Token``
(``secret_token`` из ``setWebhook``): без него апдейт подделал бы любой желающий.
Обработка вынесена в фон (``TG_WEBHOOK_FAST_ACK=true``, по умолчанию): Telegram
ждёт HTTP-ответ и повторяет апдейт, если бот молчит слишком долго, а выдача
подписки в панели занимает до 20 секунд.

Маршруты регистрируются по фактически включённым режимам — ЮKassa и Telegram
могут работать вместе и слушают один порт ``WEBHOOK_PORT``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web

from app.config import Config
from app.db import Database
from app.logctx import log_context
from app.services.payments import PaymentError, YooKassaClient
from app.services.subs import complete_order, notify_admins
from app.utils import esc

log = logging.getLogger(__name__)

__all__ = ["build_webhook_app", "drain_pending_updates", "start_webhook_server"]

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


def _secret_ok(provided: str, expected: str) -> bool:
    """Совпадает ли ``secret_token`` запроса с ожидаемым (сравнение без утечки по времени)."""
    if not expected:
        return False
    return secrets.compare_digest(provided.encode(), expected.encode())


async def _order_by_metadata(db: Database, payment: dict):
    """Заказ по ``metadata.order_id`` платежа.

    Нужен как «страховка»: если счёт по каким-то причинам перестал быть виден
    по ``orders.payment_id``/``orders.payment_ids``, заказ всё равно находится —
    иначе оплата потерялась бы (деньги списаны, период не выдан).
    """
    meta = payment.get("metadata") or {}
    try:
        order_id = int(meta.get("order_id"))
    except (TypeError, ValueError):
        return None
    return await db.get_order(order_id)


#: Задачи фоновой обработки апдейтов (нужны для graceful shutdown).
_pending_updates: set[asyncio.Task] = set()


def _spawn_update(dp: Dispatcher, bot: Bot, update: Update) -> None:
    """Обрабатывает апдейт в фоне, не задерживая HTTP-ответ Telegram."""

    async def _run() -> None:
        try:
            await dp.feed_update(bot, update)
        except Exception as exc:  # noqa: BLE001 — апдейт уже подтверждён, ронять нечем
            log.exception("Ошибка обработки апдейта %s: %s", update.update_id, exc)

    task = asyncio.get_running_loop().create_task(_run())
    _pending_updates.add(task)
    task.add_done_callback(_pending_updates.discard)


async def drain_pending_updates(timeout: float = 20.0) -> None:
    """Дожидается фоновой обработки принятых апдейтов (при остановке бота)."""
    if not _pending_updates:
        return
    log.info("Дожидаюсь обработки принятых апдейтов: %s", len(_pending_updates))
    _, still_running = await asyncio.wait(set(_pending_updates), timeout=timeout)
    if still_running:
        log.warning("Не дождались обработки %s апдейтов за %.0f с", len(still_running), timeout)


def build_webhook_app(cfg: Config, db: Database, bot: Bot, yk: YooKassaClient | None,
                      dp: Dispatcher | None = None) -> web.Application:
    """Собирает aiohttp-приложение с маршрутами включённых вебхуков."""

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

        order = await db.order_by_payment(payment_id)
        if order is None:
            # Счёт мог «отвязаться» от заказа (например, его перезаписал другой
            # счёт по тому же заказу). Ищем заказ по metadata.order_id самого
            # платежа — иначе деньги остались бы списаны, а период не выдан.
            order = await _order_by_metadata(db, payment)
            if order is None:
                log.warning("Платёж %s не привязан ни к одному заказу", payment_id)
                return web.Response(status=200, text="unknown payment")
            log.warning("Платёж %s не закреплён за заказом #%s — восстанавливаем связь",
                        payment_id, order["id"])
            await notify_admins(
                bot, cfg,
                f"🔎 Платёж <code>{esc(payment_id)}</code> восстановлен и привязан "
                f"к заказу #{order['id']} (счёт был потерян — проверьте историю платежей).",
            )

        if order["status"] == "paid" and not order["grant_error"]:
            if payment_id != (order["payment_id"] or ""):
                # По одному заказу прошло два разных платежа — деньги списаны
                # дважды; сообщаем админам (авто-возврата в боте нет).
                log.warning("Заказ #%s уже оплачен; пришёл ещё платёж %s (зачёт: %s)",
                            order["id"], payment_id, order["payment_id"])
                await notify_admins(
                    bot, cfg,
                    f"⚠️ Двойная оплата заказа #{order['id']}: платёж "
                    f"<code>{esc(payment_id)}</code> при уже оплаченном "
                    f"<code>{esc(order['payment_id'])}</code>. Нужно вернуть или учесть вручную.",
                )
            # Оплата уже обработана и подписка выдана — повторять нечего.
            return web.Response(status=200, text="already paid")

        if not yk.payment_is_valid(payment, int(order["amount"])):
            log.warning("Платёж %s не подтверждён через API — пропускаем", payment_id)
            return web.Response(status=200, text="not verified")

        try:
            await complete_order(bot, cfg, db, int(order["id"]), payment_id=payment_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка выдачи по платежу %s: %s", payment_id, exc)
            return web.Response(status=500, text="retry later")

        return web.Response(status=200, text="ok")

    async def handle_telegram(request: web.Request) -> web.Response:
        with log_context():
            return await _process_telegram(request)

    async def _process_telegram(request: web.Request) -> web.Response:
        # Подлинность запроса: secret_token знают только Telegram и бот.
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not _secret_ok(header, cfg.tg_webhook_secret):
            log.warning("Апдейт Telegram отклонён: неверный secret token (IP %s)", request.remote)
            return web.Response(status=403, text="forbidden")
        if dp is None:
            log.error("Апдейт Telegram пришёл, но диспетчер не подключён к вебхукам")
            return web.Response(status=500, text="dispatcher not attached")

        raw = await request.text()
        try:
            update = Update.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001 — мусор в теле запроса
            log.warning("Некорректный апдейт Telegram: %s", exc)
            return web.Response(status=400, text="bad update")

        if cfg.tg_webhook_fast_ack:
            # Отвечаем Telegram сразу, а обработку (до 20 с в панели/ЮKassa)
            # ведём в фоне: иначе Telegram считает доставку неудачной и
            # присылает тот же апдейт повторно.
            _spawn_update(dp, bot, update)
            return web.Response(status=200, text="ok")
        try:
            await dp.feed_update(bot, update)
        except Exception as exc:  # noqa: BLE001 — просим Telegram повторить
            log.exception("Ошибка обработки апдейта %s: %s", update.update_id, exc)
            return web.Response(status=500, text="retry later")
        return web.Response(status=200, text="ok")

    async def handle_health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "opvpn webhook"})

    app = web.Application()
    if cfg.webhook_enabled:
        app.router.add_post(cfg.webhook_path, handle_yookassa)
    if cfg.tg_webhook_enabled:
        app.router.add_post(cfg.tg_webhook_path, handle_telegram)
    # /healthz оставлен для обратной совместимости: внешний монитор смотрит
    # на отдельный порт healthcheck (см. app/health.py).
    app.router.add_get("/healthz", handle_health)
    return app


async def start_webhook_server(cfg: Config, db: Database, bot: Bot, yk: YooKassaClient | None,
                               dp: Dispatcher | None = None):
    """Поднимает aiohttp-сервер с маршрутами вебхуков. Возвращает AppRunner."""
    app = build_webhook_app(cfg, db, bot, yk, dp)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port)
    await site.start()
    log.info(
        "HTTP-сервер вебхуков на %s:%s (ЮKassa: %s, Telegram: %s)",
        cfg.webhook_host, cfg.webhook_port,
        cfg.webhook_path if cfg.webhook_enabled else "выключен",
        cfg.tg_webhook_path if cfg.tg_webhook_enabled else "выключен",
    )
    return runner
