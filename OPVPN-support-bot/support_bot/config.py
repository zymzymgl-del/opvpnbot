"""Environment-backed configuration for the standalone support bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _admin_ids(raw: str) -> tuple[int, ...]:
    values: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if item:
            try:
                value = int(item)
            except ValueError as exc:
                raise ValueError("SUPPORT_ADMIN_IDS must be comma-separated Telegram IDs") from exc
            if value <= 0:
                raise ValueError("Support admin IDs must be positive")
            values.add(value)
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: tuple[int, ...]
    database_path: Path
    opvpn_database_path: Path | None
    opvpn_panel_api_url: str | None
    opvpn_panel_api_token: str | None
    yookassa_shop_id: str | None
    yookassa_secret_key: str | None
    deepseek_api_key: str
    deepseek_model: str = "deepseek-flash"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("SUPPORT_BOT_TOKEN", "").strip()
        admins = _admin_ids(os.getenv("SUPPORT_ADMIN_IDS", ""))
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
        database_path = Path(os.getenv("SUPPORT_DB_PATH", str(BASE_DIR / "data" / "support.sqlite3")))
        if not database_path.is_absolute():
            database_path = BASE_DIR / database_path
        opvpn_database_raw = os.getenv("OPVPN_DB_PATH", "").strip()
        opvpn_database_path = Path(opvpn_database_raw) if opvpn_database_raw else None
        if opvpn_database_path is not None and not opvpn_database_path.is_absolute():
            opvpn_database_path = BASE_DIR / opvpn_database_path
        opvpn_panel_api_url = os.getenv("OPVPN_PANEL_STATUS_API_URL", "").strip().rstrip("/") or None
        opvpn_panel_api_token = os.getenv("OPVPN_PANEL_STATUS_API_TOKEN", "").strip() or None
        yookassa_shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip() or None
        yookassa_secret_key = os.getenv("YOOKASSA_SECRET_KEY", "").strip() or None
        log_level = os.getenv("SUPPORT_LOG_LEVEL", "INFO").strip().upper()

        problems: list[str] = []
        if not token:
            problems.append("SUPPORT_BOT_TOKEN is required")
        if not admins:
            problems.append("SUPPORT_ADMIN_IDS must contain at least one Telegram ID")
        if not deepseek_api_key:
            problems.append("DEEPSEEK_API_KEY is required for the AI dispatcher")
        if not deepseek_model:
            problems.append("DEEPSEEK_MODEL must not be empty")
        if bool(opvpn_panel_api_url) != bool(opvpn_panel_api_token):
            problems.append(
                "OPVPN_PANEL_STATUS_API_URL and OPVPN_PANEL_STATUS_API_TOKEN must be set together"
            )
        if bool(yookassa_shop_id) != bool(yookassa_secret_key):
            problems.append("YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY must be set together")
        if opvpn_panel_api_url and not opvpn_database_path:
            problems.append("OPVPN_DB_PATH is required when panel status integration is enabled")
        if yookassa_shop_id and not opvpn_database_path:
            problems.append("OPVPN_DB_PATH is required when direct YooKassa checks are enabled")
        if opvpn_panel_api_url:
            parsed_api_url = urlsplit(opvpn_panel_api_url)
            loopback_hosts = {"localhost", "127.0.0.1", "::1"}
            if parsed_api_url.scheme != "https" and not (
                parsed_api_url.scheme == "http" and parsed_api_url.hostname in loopback_hosts
            ):
                problems.append(
                    "OPVPN_PANEL_STATUS_API_URL must use HTTPS (HTTP is allowed only on loopback)"
                )
        if problems:
            raise ValueError("Invalid support bot configuration: " + "; ".join(problems))
        return cls(
            token,
            admins,
            database_path,
            opvpn_database_path,
            opvpn_panel_api_url,
            opvpn_panel_api_token,
            yookassa_shop_id,
            yookassa_secret_key,
            deepseek_api_key,
            deepseek_model,
            log_level,
        )

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids
