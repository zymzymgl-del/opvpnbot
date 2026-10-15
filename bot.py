#!/usr/bin/env python3
"""OPVPN — точка входа.

Запуск:  python bot.py     (или systemd-сервис opvpnbot, см. install.sh)

Порядок работы:
1. читаем .env, проверяем конфигурацию;
2. подключаем SQLite, применяем рантайм-настройки;
3. собираем диспетчер aiogram (мидлвари + роутеры пользователя и админа);
4. поднимаем HTTP-сервер вебхуков (уведомления ЮKassa и/или апдейты Telegram);
5. при TG_WEBHOOK_ENABLED=true регистрируем вебхук в Telegram (апдейты идут на
   HTTP-сервер), иначе крутим long-polling; параллельно работает фоновый воркер
   (напоминания, истечение, проверка платежей).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from app import __author__, __author_url__, __build__
from app.config import load_config, validate
from app.db import Database
from app.handlers import admin as admin_handlers
from app.handlers import user as user_handlers
from app.health import start_health_server
from app.logctx import RequestIdFilter
from app.middlewares import (
    ErrorsMiddleware,
    RequiredChannelMiddleware,
    ThrottlingMiddleware,
    UserMiddleware,
)
from app.runtime import runtime
from app.services.panels import close_panel, get_panel
from app.services.payments import YooKassaClient
from app.services.subs import background_worker, notify_admins
from app.webhook import drain_pending_updates, start_webhook_server

log = logging.getLogger("opvpnbot")


def setup_logging(level: str, log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
                                            encoding="utf-8"))
    except OSError as exc:  # нет прав на запись — работаем только в stdout
        print(f"Не удалось открыть лог-файл {log_file}: {exc}", file=sys.stderr)
    rid_filter = RequestIdFilter()
    for handler in handlers:
        handler.addFilter(rid_filter)
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | rid=%(request_id)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="menu", description="🛒 Купить VPN"),
        BotCommand(command="help", description="ℹ️ Помощь"),
        BotCommand(command="terms", description="📄 Условия использования"),
        BotCommand(command="privacy", description="🔒 Конфиденциальность"),
    ])


def build_dispatcher(cfg, db: Database, bot: Bot, yk) -> Dispatcher:
    """Собирает диспетчер: мидлвари, роутеры, данные для хендлеров."""
    dp = Dispatcher()
    dp["cfg"] = cfg
    dp["db"] = db
    dp["yk"] = yk

    # --- мидлвари ---
    dp.update.outer_middleware(ErrorsMiddleware(cfg))
    throttling = ThrottlingMiddleware(cfg.throttling_rate)
    dp.message.middleware(throttling)
    dp.callback_query.middleware(throttling)
    dp.message.middleware(UserMiddleware(db, cfg))
    dp.callback_query.middleware(UserMiddleware(db, cfg))
    channel_guard = RequiredChannelMiddleware(bot, cfg)
    dp.message.middleware(channel_guard)
    dp.callback_query.middleware(channel_guard)

    # --- роутеры: админские раньше пользовательских ---
    dp.include_router(admin_handlers.router)
    dp.include_router(user_handlers.router)
    return dp


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level, cfg.log_file)

    problems = validate(cfg)
    if problems:
        log.error("Проблемы конфигурации:\n  • %s", "\n  • ".join(problems))
        log.error("Исправьте .env и запустите бота снова (образец — .env.example).")
        raise SystemExit(2)

    db = Database(cfg.db_path)
    await db.connect()
    await runtime.load(db, cfg)

    # Прокси для Telegram (SOCKS5/HTTP). Указывается в .env как
    # TELEGRAM_PROXY=...; fallback на PAYMENTS_PROXY, если задан только он.
    # Пусто — ходим напрямую.
    # Таймаут HTTP-запросов к Telegram API — TG_REQUEST_TIMEOUT: при нестабильной
    # сети/прокси стандартных 60 с не хватает и вылазят
    # TelegramNetworkError("Request timeout error").
    tg_proxy = (cfg.telegram_proxy or cfg.payments_proxy or "").strip()
    session = AiohttpSession(proxy=tg_proxy or None, timeout=cfg.tg_request_timeout)
    if tg_proxy:
        log.info("Telegram-транспорт: через прокси %s (таймаут %s с)",
                 tg_proxy.split("@", 1)[-1],  # хост:порт без учётки
                 cfg.tg_request_timeout)
    else:
        log.info("Telegram-транспорт: напрямую (таймаут %s с)", cfg.tg_request_timeout)

    bot = Bot(cfg.bot_token,
              session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("Бот @%s (id=%s), версия %s; основа — DF VPN Bot (@%s, %s)", me.username, me.id, __build__, __author__, __author_url__)

    yk = YooKassaClient(cfg) if cfg.yookassa_enabled else None
    if cfg.yookassa_enabled:
        log.info("Оплата картой/СБП: включена (магазин %s)", cfg.yookassa_shop_id)

    dp = build_dispatcher(cfg, db, bot, yk)
    dp["bot_username"] = me.username

    # --- HTTP-сервер: вебхуки ЮKassa и/или апдейты Telegram ---
    webhook_runner = None
    if cfg.any_webhook_enabled:
        try:
            webhook_runner = await start_webhook_server(cfg, db, bot, yk, dp)
        except OSError as exc:
            log.error("Не удалось запустить сервер вебхуков на %s:%s — %s",
                      cfg.webhook_host, cfg.webhook_port, exc)
            log.error("Платежи будут подтверждаться опросом API (раз в 3 минуты), "
                      "апдейты Telegram — запросами getUpdates.")

    # --- транспорт апдейтов Telegram: вебхук или long-polling -----------------
    # Сначала пробуем вебхук; если он недоступен (нет HTTP-сервера или Telegram
    # отверг URL) — снимаем вебхук и работаем через getUpdates, иначе бот вообще
    # не получал бы апдейты.
    tg_webhook_active = False
    if cfg.tg_webhook_enabled and webhook_runner is not None:
        try:
            await bot.set_webhook(
                url=cfg.tg_webhook_url,
                secret_token=cfg.tg_webhook_secret,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=False,
            )
            info = await bot.get_webhook_info()
            tg_webhook_active = True
            log.info("Апдейты Telegram: вебхук %s (ожидает доставки: %s, ошибок: %s)",
                     info.url, info.pending_update_count, info.last_error_message or "нет")
        except TelegramAPIError as exc:
            log.error("Не удалось установить вебхук Telegram (%s). Проверьте TG_WEBHOOK_URL: "
                      "Telegram принимает только HTTPS с валидным сертификатом.", exc)

    if not tg_webhook_active:
        if cfg.tg_webhook_enabled:
            log.error("Вебхук Telegram не работает — снимаю его и перехожу на long-polling, "
                      "чтобы бот продолжал получать апдейты.")
        # Вебхук мог остаться от прошлой конфигурации: пока он установлен,
        # getUpdates падает с «Conflict: can't use getUpdates method while webhook is active».
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except TelegramAPIError as exc:
            log.warning("Не удалось снять ранее установленный вебхук Telegram: %s", exc)

    # --- healthcheck для внешнего мониторинга (uptime) ---------------------
    health_runner = None
    if cfg.health_enabled:
        try:
            health_runner = await start_health_server(cfg, db)
        except OSError as exc:
            log.error("Не удалось запустить healthcheck на %s:%s — %s",
                      cfg.health_host, cfg.health_port, exc)

    # --- фоновая обработка (напоминания, истечение, проверка платежей) ---
    worker = asyncio.create_task(background_worker(bot, cfg, db, yk=yk), name="background-worker")

    # --- проверка панели на старте ---
    try:
        panel = get_panel(cfg, db)
        if cfg.panel == "xui":
            log.info(
                "3x-ui TLS: проверка сертификата %s%s",
                "включена" if cfg.xui_verify_ssl else "ОТКЛЮЧЕНА (XUI_VERIFY_SSL=false)",
                f", CA: {cfg.xui_ca_file}" if cfg.xui_ca_file else "",
            )
        ok, message = await panel.health()
        log.info("Панель '%s': %s — %s", cfg.panel, "OK" if ok else "ПРОБЛЕМА", message)
        if not ok:
            await notify_admins(bot, cfg, f"⚠️ Панель выдачи '{cfg.panel}': {message}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Проверка панели не удалась: %s", exc)

    await set_commands(bot)
    await notify_admins(
        bot, cfg,
        f"🚀 <b>Бот запущен</b> (v{__build__})\n\n"
        f"Оплата: <code>{', '.join(cfg.payment_methods)}</code>",
    )

    stop_event = asyncio.Event()

    def _stop(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover
            pass

    async def _watch_stop() -> None:
        await stop_event.wait()
        log.info("Получен сигнал остановки — завершаем работу…")
        worker.cancel()
        if not tg_webhook_active:
            await dp.stop_polling()

    watcher = asyncio.create_task(_watch_stop())

    try:
        if tg_webhook_active:
            # Апдейты приходят на HTTP-сервер: здесь только ждём сигнала остановки.
            # Вебхук намеренно НЕ снимаем: пока служба перезапускается, Telegram
            # будет повторять доставку и ни один апдейт не потеряется.
            await stop_event.wait()
        else:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher.cancel()
        worker.cancel()
        # Апдейты, принятые с «быстрым ack», обрабатываются в фоне — даём им
        # завершиться, иначе на остановке теряются (Telegram уже получил 200).
        await drain_pending_updates()
        if webhook_runner is not None:
            await webhook_runner.cleanup()
        if health_runner is not None:
            await health_runner.cleanup()
        if yk is not None:
            await yk.close()
        await close_panel()
        await db.close()
        await bot.session.close()
        log.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code not in (0, None):
            sys.exit(exc.code)
