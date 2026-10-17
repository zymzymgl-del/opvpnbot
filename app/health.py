"""HTTP-эндпоинт здоровья для внешнего мониторинга (uptime).

Поднимает минимальный aiohttp-сервер с ``GET /healthz``, доступный **независимо**
от того, включены ли вебхуки ЮKassa (у polling-развёртываний своего HTTP-сервера
иначе нет). Внешний монитор (healthchecks.io, UptimeRobot, Better Stack) опрашивает
этот адрес и присылает алерт, если бот перестал отвечать — это закрывает слепую
зону «процесс упал и никто не заметил».

Проверки:

* **БД** — всегда: ``SELECT 1``. Недоступная БД = 503 (как и раньше).
* **Панель 3x-ui** — с кэшем ``HEALTH_PANEL_TTL`` (по умолчанию 60 с), чтобы
  монитор не дёргал панель на каждый запрос. Отключается ``HEALTH_PANEL_CHECK=false``.
* **Heartbeat** — когда последний раз обрабатывался апдейт Telegram
  (``touch_update`` из мидлвари) и когда последний раз тикал фоновый воркер
  (``touch_worker`` из ``background_worker``).

Обратная совместимость: поля ``ok``/``db``/``version``/``build`` сохранены, а
HTTP-статус по умолчанию (``HEALTH_DEEP=false``) по-прежнему зависит только от
БД. Чтобы панель и heartbeat влияли на статус, включите ``HEALTH_DEEP=true``
(тогда проблемы дают 503 и попадают в ``reasons``).

Сервер по умолчанию слушает ``127.0.0.1`` (безопасно для systemd-развёртывания:
монитор/nginx ходит на localhost). Для Docker задайте ``HEALTH_HOST=0.0.0.0`` и
пробросьте порт, либо проксируйте через nginx.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from aiohttp import web

from app import __build__, __version__
from app.config import Config
from app.db import Database
from app.services.panels import get_panel

log = logging.getLogger(__name__)

# --- heartbeat -------------------------------------------------------------
# Монотонные метки (не зависят от перевода часов). Инициализируем временем
# импорта, чтобы сразу после старта процесса health не «протухал» до первого
# апдейта/тика воркера.
_started_at = time.monotonic()
_last_update_at = _started_at
_last_worker_tick = _started_at


def touch_update() -> None:
    """Отметить обработку апдейта Telegram (вызывается из ErrorsMiddleware)."""
    global _last_update_at
    _last_update_at = time.monotonic()


def touch_worker() -> None:
    """Отметить итерацию фонового воркера (напоминания/платежи/бэкапы)."""
    global _last_worker_tick
    _last_worker_tick = time.monotonic()


def _age(moment: float) -> float:
    return max(0.0, time.monotonic() - moment)


def heartbeat() -> dict[str, float]:
    """Возраст (сек) последнего апдейта и последнего тика воркера."""
    return {
        "update_age": round(_age(_last_update_at), 1),
        "worker_age": round(_age(_last_worker_tick), 1),
    }


# --- проверка панели с кэшем ----------------------------------------------
#: (monotonic-время проверки, ok, сообщение)
_panel_cache: Optional[tuple[float, bool, str]] = None
_panel_lock: Optional[asyncio.Lock] = None
_last_panel_ok_logged: Optional[bool] = None


def reset_state() -> None:
    """Сбрасывает heartbeat и кэш панели (используется в тестах)."""
    global _last_update_at, _last_worker_tick, _panel_cache, _panel_lock
    global _last_panel_ok_logged
    now = time.monotonic()
    _last_update_at = now
    _last_worker_tick = now
    _panel_cache = None
    _panel_lock = None
    _last_panel_ok_logged = None


async def _check_panel_cached(cfg: Config, db: Database) -> dict[str, Any]:
    """Проверяет панель не чаще раза в ``HEALTH_PANEL_TTL`` секунд.

    Результат (включая ошибку) кэшируется: если панель лежит, монитор с
    интервалом в минуту не должен создавать по запросу в 3x-ui.
    """
    global _panel_cache, _panel_lock, _last_panel_ok_logged

    if not cfg.health_panel_check:
        return {"ok": None, "message": "проверка выключена", "checked_ago": None}

    if _panel_lock is None:
        _panel_lock = asyncio.Lock()

    ttl = max(0, cfg.health_panel_ttl)
    now = time.monotonic()
    cached = _panel_cache
    if cached is not None and now - cached[0] < ttl:
        return {
            "ok": cached[1],
            "message": cached[2],
            "checked_ago": round(now - cached[0], 1),
        }

    async with _panel_lock:
        # Пока ждали лок, соседний запрос мог уже обновить кэш.
        now = time.monotonic()
        cached = _panel_cache
        if cached is not None and now - cached[0] < ttl:
            return {
                "ok": cached[1],
                "message": cached[2],
                "checked_ago": round(now - cached[0], 1),
            }

        ok = False
        message = ""
        try:
            panel = get_panel(cfg, db)
            ok, message = await asyncio.wait_for(
                panel.health(), timeout=cfg.health_panel_timeout
            )
            ok = bool(ok)
            message = str(message)
        except asyncio.TimeoutError:
            message = f"панель не ответила за {cfg.health_panel_timeout:g} с"
        except Exception as exc:  # noqa: BLE001 — health не должен падать никогда
            message = f"{type(exc).__name__}: {exc}"

        _panel_cache = (now, ok, message)
        # Логируем только смену состояния, иначе каждый опрос монитора
        # превратился бы в строку лога.
        if ok != _last_panel_ok_logged:
            if ok:
                log.info("healthcheck: панель доступна — %s", message)
            else:
                log.warning("healthcheck: панель недоступна — %s", message)
            _last_panel_ok_logged = ok
        return {"ok": ok, "message": message, "checked_ago": 0.0}


def evaluate(cfg: Config, *, db_ok: bool, panel: dict[str, Any],
             hb: dict[str, float]) -> tuple[bool, list[str]]:
    """Итоговый статус и список причин отказа (чистая функция — удобно тестировать).

    В обычном режиме (``HEALTH_DEEP=false``) статус зависит только от БД, как
    до появления расширенных проверок. В глубоком режиме панель и «зависшие»
    heartbeat тоже дают 503.
    """
    healthy = bool(db_ok)
    reasons: list[str] = [] if db_ok else ["db"]
    if not cfg.health_deep:
        return healthy, reasons

    if panel.get("ok") is False:
        healthy = False
        reasons.append("panel")
    if cfg.health_max_worker_age > 0 and hb["worker_age"] > cfg.health_max_worker_age:
        healthy = False
        reasons.append("worker")
    if cfg.health_max_update_age > 0 and hb["update_age"] > cfg.health_max_update_age:
        healthy = False
        reasons.append("updates")
    return healthy, reasons


def build_health_app(cfg: Config, db: Database) -> web.Application:
    """Собирает aiohttp-приложение с маршрутом ``/healthz``."""

    async def handle_health(_: web.Request) -> web.Response:
        db_ok = True
        db_error = ""
        try:
            await db.scalar("SELECT 1", default=None)
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            db_error = f"{type(exc).__name__}: {exc}"
            log.warning("healthcheck: БД недоступна: %s", exc)

        panel = await _check_panel_cached(cfg, db)
        hb = heartbeat()
        healthy, reasons = evaluate(cfg, db_ok=db_ok, panel=panel, hb=hb)

        payload: dict[str, Any] = {
            # Совместимость со старыми мониторами: ok/db/version/build.
            "ok": healthy,
            "db": "ok" if db_ok else "error",
            "version": __version__,
            "build": __build__,
            "panel": (
                "disabled" if panel["ok"] is None else ("ok" if panel["ok"] else "error")
            ),
            "panel_message": panel["message"],
            "panel_checked_ago": panel["checked_ago"],
            "heartbeat": hb,
            "deep": cfg.health_deep,
        }
        if db_error:
            payload["db_error"] = db_error
        if reasons:
            payload["reasons"] = reasons
        return web.json_response(payload, status=200 if healthy else 503)

    app = web.Application()
    app.router.add_get("/healthz", handle_health)
    return app


async def start_health_server(cfg: Config, db: Database) -> Optional[web.AppRunner]:
    """Запускает health-сервер, если он включён. Возвращает runner для остановки."""
    if not cfg.health_enabled:
        return None

    runner = web.AppRunner(build_health_app(cfg, db), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.health_host, cfg.health_port)
    await site.start()
    log.info(
        "Healthcheck слушаем на http://%s:%s/healthz (deep=%s, панель=%s)",
        cfg.health_host, cfg.health_port,
        "вкл" if cfg.health_deep else "выкл",
        "проверяется" if cfg.health_panel_check else "выключена",
    )
    return runner


__all__ = [
    "build_health_app",
    "start_health_server",
    "touch_update",
    "touch_worker",
    "heartbeat",
    "evaluate",
    "reset_state",
]

