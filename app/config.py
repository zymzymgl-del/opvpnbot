"""Конфигурация бота: чтение, разбор и валидация переменных окружения.

Все настройки берутся из .env (см. .env.example) либо из переменных окружения
(например, когда бот запущен как systemd-сервис с EnvironmentFile).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Мягкая загрузка .env: если установлен python-dotenv — используем его."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_file)
        return
    except Exception:  # pragma: no cover - fallback без зависимости
        pass
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "да"}


def _ints(name: str, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    result: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.append(int(chunk))
        except ValueError:
            continue
    return tuple(result) or default


def _list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.replace(";", ",").split(",") if part.strip())


def _xui_inbound_ids() -> tuple[int, ...]:
    """Inbound'ы 3x-ui для выдачи: XUI_INBOUND_IDS (через запятую) с откатом на XUI_INBOUND_ID.

    Несколько ID — тогда панель соберёт конфиги из всех этих inbound'ов в одну
    подписку /sub/{subId} (разные шифрования/транспорты в рамках одного ключа).
    """
    ids = _ints("XUI_INBOUND_IDS")
    if ids:
        return ids
    single = _int("XUI_INBOUND_ID", 0)
    return (single,) if single else ()


def is_absolute_url(value: str) -> bool:
    """True, если значение — абсолютный http(s)-URL с непустым хостом.

    Нужно для ссылок, которые уезжают в Telegram (URL-кнопки, web_app):
    относительный путь вроде ``/hvpa9knwot91fkxb/d14e3ae64c00b83c`` Telegram
    отклоняет целиком («URL host is empty»), и тогда не отправляется всё
    сообщение с клавиатурой (карточка подписки не появляется у пользователя).
    """
    parts = urlsplit(value or "")
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    bot_token: str
    telegram_proxy: str
    admin_ids: tuple[int, ...]
    news_channel_url: str
    required_channels: tuple[str, ...]
    bot_name: str

    # --- Тарифы / продажи ---
    currency: str
    trial_enabled: bool
    trial_days: int
    trial_devices: int
    trial_traffic_gb: int
    trial_panel_hint: str
    max_pending_orders: int

    # --- Оплата ---
    payment_methods: tuple[str, ...]
    yookassa_shop_id: str
    yookassa_secret_key: str
    yookassa_return_url: str
    yookassa_test: bool
    manual_payment_note: str
    payments_proxy: str

    # --- Панель выдачи ключей ---
    panel: str
    xui_url: str
    xui_username: str
    xui_password: str
    xui_api_token: str
    xui_inbound_id: int
    xui_inbound_ids: tuple[int, ...]
    xui_public_host: str
    xui_sub_base: str
    xui_verify_ssl: bool
    xui_ca_file: str

    # --- Привязка существующих клиентов панели (только xui) ---
    claim_enabled: bool
    claim_secret: str
    claim_ttl_hours: int

    # --- Вебхуки ЮKassa ---
    webhook_enabled: bool
    webhook_host: str
    webhook_port: int
    webhook_path: str
    webhook_public_url: str
    webhook_trust_proxy: bool

    # --- Вебхук Telegram (вместо long-polling) ---
    tg_webhook_enabled: bool
    tg_webhook_url: str
    tg_webhook_secret: str
    tg_webhook_fast_ack: bool

    # --- Healthcheck (внешний мониторинг) ---
    health_enabled: bool
    health_host: str
    health_port: int
    #: Включать панель и heartbeat в HTTP-статус (false — только диагностика в JSON).
    health_deep: bool
    health_panel_check: bool
    #: Сколько секунд кэшировать результат проверки панели (0 — проверять каждый раз).
    health_panel_ttl: int
    health_panel_timeout: float
    #: Пороги heartbeat: 0 — проверка выключена.
    health_max_worker_age: int
    health_max_update_age: int

    # --- Прочее ---
    db_path: str
    log_level: str
    log_file: str
    auto_disable_expired: bool
    notify_days_before: tuple[int, ...]
    orders_ttl_hours: int
    invoice_ttl_minutes: int
    backup_interval_minutes: int
    backup_keep: int
    throttling_rate: float
    #: Таймаут HTTP-запросов к Telegram API (сек). Увеличьте при нестабильной
    #: сети/прокси, иначе длинные операции падают с TelegramNetworkError
    #: ("HTTP Client says - Request timeout error").
    tg_request_timeout: float = field(default=60.0)
    terms_text: str = ""
    privacy_text: str = ""

    admins_only: bool = field(default=False)

    @property
    def yookassa_enabled(self) -> bool:
        return "yookassa" in self.payment_methods and bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def tg_webhook_path(self) -> str:
        """Локальный путь, который слушает бот (берётся из публичного URL).

        Один источник правды: куда nginx проксирует запросы, туда же смотрит
        маршрут aiohttp — расхождение путей ломало бы приём апдейтов.
        """
        path = urlsplit(self.tg_webhook_url).path or ""
        return path if len(path) > 1 else "/tg/webhook"

    @property
    def any_webhook_enabled(self) -> bool:
        """Нужно ли поднимать общий HTTP-сервер (ЮKassa и/или Telegram)."""
        return self.webhook_enabled or self.tg_webhook_enabled

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load_config() -> Config:
    cfg = Config(
        bot_token=_str("BOT_TOKEN"),
        telegram_proxy=_str("TELEGRAM_PROXY"),
        admin_ids=_ints("ADMIN_IDS") or _ints("ADMIN_CHAT_ID"),
        news_channel_url=_str("NEWS_CHANNEL_URL"),
        required_channels=_list("REQUIRED_CHANNELS"),
        bot_name=_str("BOT_NAME", "OPVPN"),
        currency=_str("CURRENCY", "₽"),
        trial_enabled=_bool("TRIAL_ENABLED", False),
        trial_days=_int("TRIAL_DAYS", 3),
        trial_devices=_int("TRIAL_DEVICES", 1),
        trial_traffic_gb=_int("TRIAL_TRAFFIC_GB", 10),
        trial_panel_hint=_str("TRIAL_PANEL_HINT", "trial"),
        max_pending_orders=_int("MAX_PENDING_ORDERS", 5),
        payment_methods=_list("PAYMENT_METHODS", ("yookassa",)),
        yookassa_shop_id=_str("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=_str("YOOKASSA_SECRET_KEY"),
        yookassa_return_url=_str("YOOKASSA_RETURN_URL", "https://t.me"),
        yookassa_test=_bool("YOOKASSA_TEST", False),
        manual_payment_note=_str("MANUAL_PAYMENT_NOTE", "Реквизиты уточняйте у поддержки."),
        payments_proxy=_str("PAYMENTS_PROXY"),
        panel=_str("VPN_PANEL", "xui").lower(),
        xui_url=_str("XUI_URL").rstrip("/"),
        xui_username=_str("XUI_USERNAME", "admin"),
        xui_password=_str("XUI_PASSWORD"),
        xui_api_token=_str("XUI_API_TOKEN"),
        xui_inbound_id=_int("XUI_INBOUND_ID", 0),
        xui_inbound_ids=_xui_inbound_ids(),
        xui_public_host=_str("XUI_PUBLIC_HOST"),
        xui_sub_base=_str("XUI_SUB_BASE").rstrip("/"),
        xui_verify_ssl=_bool("XUI_VERIFY_SSL", True),
        xui_ca_file=_str("XUI_CA_FILE"),
        claim_enabled=_bool("CLAIM_ENABLED", False),
        claim_secret=_str("CLAIM_SECRET"),
        claim_ttl_hours=max(1, _int("CLAIM_TTL_HOURS", 168)),
        webhook_enabled=_bool("WEBHOOK_ENABLED", False),
        webhook_host=_str("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=_int("WEBHOOK_PORT", 8080),
        webhook_path=_str("WEBHOOK_PATH", "/yookassa/webhook"),
        webhook_public_url=_str("WEBHOOK_PUBLIC_URL"),
        webhook_trust_proxy=_bool("WEBHOOK_TRUST_PROXY", False),
        tg_webhook_enabled=_bool("TG_WEBHOOK_ENABLED", False),
        tg_webhook_url=_str("TG_WEBHOOK_URL").rstrip("/"),
        tg_webhook_secret=_str("TG_WEBHOOK_SECRET"),
        tg_webhook_fast_ack=_bool("TG_WEBHOOK_FAST_ACK", True),
        health_enabled=_bool("HEALTH_ENABLED", True),
        health_host=_str("HEALTH_HOST", "127.0.0.1"),
        health_port=_int("HEALTH_PORT", 8081),
        health_deep=_bool("HEALTH_DEEP", False),
        health_panel_check=_bool("HEALTH_PANEL_CHECK", True),
        health_panel_ttl=max(0, _int("HEALTH_PANEL_TTL", 60)),
        health_panel_timeout=_float("HEALTH_PANEL_TIMEOUT", 5.0),
        health_max_worker_age=max(0, _int("HEALTH_MAX_WORKER_AGE", 600)),
        health_max_update_age=max(0, _int("HEALTH_MAX_UPDATE_AGE", 0)),
        db_path=_str("DB_PATH", str(BASE_DIR / "data" / "opvpnbot.sqlite3")),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        log_file=_str("LOG_FILE", str(BASE_DIR / "data" / "bot.log")),
        auto_disable_expired=_bool("AUTO_DISABLE_EXPIRED", True),
        notify_days_before=tuple(sorted({d for d in _ints("NOTIFY_DAYS_BEFORE", (3, 1)) if d > 0}, reverse=True)),
        orders_ttl_hours=_int("ORDERS_TTL_HOURS", 48),
        invoice_ttl_minutes=_int("INVOICE_TTL_MINUTES", 120),
        backup_interval_minutes=_int("BACKUP_INTERVAL_MINUTES", 60),
        backup_keep=_int("BACKUP_KEEP", 48),
        throttling_rate=_float("THROTTLING_RATE", 0.6),
        tg_request_timeout=_float("TG_REQUEST_TIMEOUT", 60.0),
        terms_text=_str("TERMS_TEXT"),
        privacy_text=_str("PRIVACY_TEXT"),
    )
    return cfg


def validate(cfg: Config) -> list[str]:
    """Возвращает список проблем конфигурации (пустой список = всё в порядке)."""
    problems: list[str] = []
    if not cfg.bot_token:
        problems.append("BOT_TOKEN не задан — получите токен у @BotFather.")
    if not cfg.admin_ids:
        problems.append("ADMIN_IDS не задан — укажите свой Telegram ID (узнать: @userinfobot).")
    if cfg.panel != "xui":
        problems.append(f"VPN_PANEL={cfg.panel!r} неизвестен. Доступен только режим: xui.")
    if cfg.panel == "xui":
        if not cfg.xui_url:
            problems.append("VPN_PANEL=xui, но не задан XUI_URL.")
        if not (cfg.xui_api_token or (cfg.xui_username and cfg.xui_password)):
            problems.append("VPN_PANEL=xui: нужен XUI_API_TOKEN или XUI_USERNAME/XUI_PASSWORD.")
        if cfg.xui_ca_file and not Path(cfg.xui_ca_file).is_file():
            problems.append(
                f"XUI_CA_FILE={cfg.xui_ca_file!r} — файл не найден (нужен PEM-сертификат CA)."
            )
        if cfg.xui_sub_base and not is_absolute_url(cfg.xui_sub_base):
            problems.append(
                f"XUI_SUB_BASE={cfg.xui_sub_base!r} — нужен ПОЛНЫЙ адрес подписочного "
                "сервера панели, вместе со схемой и хостом, например "
                "https://panel.example.com:2096/hvpa9knwot91fkxb (в панели это «Subscription "
                "URL»). Один путь без хоста не годится: Telegram отклоняет такие ссылки "
                "в кнопках («URL host is empty») и карточка подписки не отправляется вовсе."
            )
    if cfg.claim_enabled and not cfg.claim_secret:
        problems.append("CLAIM_ENABLED=true, но не задан CLAIM_SECRET — ссылки привязки нельзя подписать.")
    if cfg.claim_enabled and cfg.panel != "xui":
        problems.append("CLAIM_ENABLED=true поддерживается только при VPN_PANEL=xui.")
    if "yookassa" in cfg.payment_methods and not cfg.yookassa_enabled:
        problems.append(
            "Способ оплаты yookassa включён, но YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы."
        )
    if not cfg.payment_methods:
        problems.append("PAYMENT_METHODS пуст — не осталось ни одного способа оплаты.")
    if cfg.webhook_enabled and not cfg.webhook_public_url:
        problems.append(
            "WEBHOOK_ENABLED=true, но WEBHOOK_PUBLIC_URL не задан — укажите публичный адрес "
            "вида https://bot.example.com/yookassa/webhook и пропишите его в кабинете ЮKassa."
        )
    if cfg.health_enabled and cfg.health_port <= 0:
        problems.append("HEALTH_PORT должен быть больше нуля (либо выключите HEALTH_ENABLED).")
    if cfg.health_enabled and cfg.health_panel_timeout <= 0:
        problems.append("HEALTH_PANEL_TIMEOUT должен быть больше нуля (например, 5).")
    if cfg.health_enabled and cfg.any_webhook_enabled and cfg.health_port == cfg.webhook_port:
        problems.append(
            "HEALTH_PORT и WEBHOOK_PORT совпадают — healthcheck и вебхуки не смогут слушать "
            "один порт. Разведите их (например, HEALTH_PORT=8081, WEBHOOK_PORT=8080)."
        )
    problems.extend(_validate_tg_webhook(cfg))
    if cfg.payments_proxy and not cfg.payments_proxy.startswith(("http://", "https://")):
        problems.append(
            "PAYMENTS_PROXY должен начинаться с http:// или https:// "
            "(aiohttp умеет только HTTP-прокси; для SOCKS нужна библиотека aiohttp-socks)."
        )
    return problems


#: secret_token Telegram: 1-256 символов, допустимы A-Z, a-z, 0-9, «_» и «-».
TG_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{8,256}")


def _validate_tg_webhook(cfg: Config) -> list[str]:
    """Проверки режима вебхука Telegram (пусто, если режим выключен)."""
    problems: list[str] = []
    if not cfg.tg_webhook_enabled:
        return problems
    if not cfg.tg_webhook_url:
        problems.append(
            "TG_WEBHOOK_ENABLED=true, но TG_WEBHOOK_URL не задан — укажите публичный адрес "
            "вида https://bot.example.com/tg/webhook (он же задаёт путь на nginx)."
        )
        return problems
    parts = urlsplit(cfg.tg_webhook_url)
    if parts.scheme != "https" or not parts.netloc:
        problems.append(
            f"TG_WEBHOOK_URL={cfg.tg_webhook_url!r} — нужен публичный адрес вида "
            "https://bot.example.com/tg/webhook: Telegram принимает только HTTPS с валидным "
            "сертификатом (самоподписанные и http отклоняет)."
        )
    elif len(parts.path) < 2:
        problems.append(
            "TG_WEBHOOK_URL должен содержать путь (например /tg/webhook): "
            "адрес без пути Telegram не примет."
        )
    elif cfg.tg_webhook_path == cfg.webhook_path:
        problems.append(
            f"TG_WEBHOOK_URL и WEBHOOK_PATH указывают на один путь {cfg.webhook_path!r} — "
            "вебхуки ЮKassa и Telegram слушают один порт, разведите пути "
            "(например /tg/webhook и /yookassa/webhook)."
        )
    if not cfg.tg_webhook_secret:
        problems.append(
            "TG_WEBHOOK_ENABLED=true, но TG_WEBHOOK_SECRET пуст — сгенерируйте секрет "
            "(openssl rand -hex 32) и впишите его в .env без кавычек, скобок и комментариев."
        )
    elif not TG_SECRET_RE.fullmatch(cfg.tg_webhook_secret):
        problems.append(
            f"TG_WEBHOOK_SECRET задан неверно: {_secret_hint(cfg.tg_webhook_secret)}. "
            "Нужен secret_token Telegram из 8-256 символов A-Z, a-z, 0-9, «_» и «-» "
            "(тот же секрет бот отправляет в setWebhook). Сгенерировать: openssl rand -hex 32"
        )
    return problems


def _secret_hint(secret: str) -> str:
    """Что именно не так с секретом — частые ошибки при вставке в .env.

    Раньше все случаи сводились к одному сообщению «задан неверно», и понять,
    что вставлен плейсхолдер из инструкции или остался пробел с комментарием,
    можно было только чтением файла.
    """
    if "<" in secret or ">" in secret:
        return "похоже, вставлен плейсхолдер вида <openssl rand -hex 32> — выполните команду и вставьте её вывод"
    if any(ch.isspace() for ch in secret):
        return "в значении есть пробел, таб или текст после пробела (например комментарий #) — уберите их"
    if not secret.isascii():
        return "есть символы вне латиницы (кириллица?) — Telegram принимает только A-Z, a-z, 0-9, «_» и «-»"
    if len(secret) < 8:
        return f"слишком короткий: {len(secret)} символов, нужно минимум 8"
    if len(secret) > 256:
        return f"слишком длинный: {len(secret)} символов, максимум 256"
    return "недопустимые символы — разрешены только A-Z, a-z, 0-9, «_» и «-»"
