"""HTTP-эндпоинт здоровья для внешнего мониторинга (uptime).

Поднимает минимальный aiohttp-сервер с ``GET /healthz``, доступный **независимо**
от того, включены ли вебхуки ЮKassa (у polling-развёртываний своего HTTP-сервера
иначе нет). Внешний монитор (healthchecks.io, UptimeRobot, Better Stack) опрашивает
этот адрес и присылает алерт, если бот перестал отвечать — это закрывает слепую
зону «процесс упал и никто не заметил».

Сервер по умолчанию слушает ``127.0.0.1`` (безопасно для systemd-развёртывания:
монитор/nginx ходит на localhost). Для Docker задайте ``HEALTH_HOST=0.0.0.0`` и
пробросьте порт, либо проксируйте через nginx.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

from app import __build__, __version__
from app.config import Config
from app.db import Database

log = logging.getLogger(__name__)


def build_health_app(db: Database) -> web.Application:
    """Собирает aiohttp-приложение с маршрутом ``/healthz``."""

    async def handle_health(_: web.Request) -> web.Response:
        # Реальная проверка зависимости: если БД недоступна, бот фактически
        # не может ни выдать подписку, ни подтвердить оплату — отдаём 503.
        try:
            await db.scalar("SELECT 1", default=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("healthcheck: БД недоступна: %s", exc)
            return web.json_response(
                {"ok": False, "db": "error", "version": __version__, "build": __build__},
                status=503,
            )
        return web.json_response(
            {"ok": True, "db": "ok", "version": __version__, "build": __build__}
        )

    app = web.Application()
    app.router.add_get("/healthz", handle_health)
    return app


async def start_health_server(cfg: Config, db: Database) -> Optional[web.AppRunner]:
    """Запускает health-сервер, если он включён. Возвращает runner для остановки."""
    if not cfg.health_enabled:
        return None

    runner = web.AppRunner(build_health_app(db), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.health_host, cfg.health_port)
    await site.start()
    log.info("Healthcheck слушаем на http://%s:%s/healthz", cfg.health_host, cfg.health_port)
    return runner


__all__ = ["build_health_app", "start_health_server"]
