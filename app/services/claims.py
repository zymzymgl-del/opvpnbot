"""Привязка готовых клиентов панели к Telegram по QR/ссылке.

Администратор создаёт клиента в панели 3x-ui, затем в админке бота получает
ссылку ``https://t.me/<bot>?start=claim_<token>`` (и QR). Токен — HMAC-SHA256
от ``subId`` клиента и метки времени окончания действия: он не равен subId
(утечки идентификатора панели нет) и ограничен по сроку жизни. Клиент открывает
ссылку — бот находит клиента по токену, привязывает его к Telegram-ID и создаёт
подписку, после чего тот продлевает её сам через бота (``panel.extend_client``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot

from app.config import Config
from app.db import Database, utcnow
from app.services.panels import ClaimedClient, get_panel
from app.services.subs import notify_admins, send_subscription_card
from app.utils import esc, fmt_dt

log = logging.getLogger(__name__)

CLAIM_PREFIX = "claim_"
#: Длина подписи токена в hex-символах.
SIG_LEN = 20
#: Срок жизни ссылки привязки (секунды). По истечении клиенту нужна новая.
CLAIM_TTL_SECONDS = 7 * 24 * 3600


def _signature(sub_id: str, exp: int, secret: str) -> str:
    message = f"{sub_id}:{exp}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:SIG_LEN]


def claim_token(sub_id: str, secret: str, *, ttl: int = CLAIM_TTL_SECONDS,
                now: Optional[float] = None) -> str:
    """Подписанный токен вида ``<exp_hex>-<sig>`` для subId клиента панели.

    Разделитель ``-`` (а не ``.``), потому что Telegram принимает в ``start``
    только символы ``A-Za-z0-9_-``.
    """
    exp = int((now if now is not None else time.time()) + ttl)
    return f"{exp:x}-{_signature(sub_id, exp, secret)}"


def parse_token(token: str) -> Optional[tuple[int, str]]:
    """Разбирает токен на (срок действия UTC-epoch, подпись). None — если формат битый."""
    exp_hex, sep, sig = token.partition("-")
    if not sep or not sig:
        return None
    try:
        exp = int(exp_hex, 16)
    except ValueError:
        return None
    return exp, sig


def token_expired(token: str, *, now: Optional[float] = None) -> bool:
    parsed = parse_token(token)
    if parsed is None:
        return True
    return parsed[0] <= int(now if now is not None else time.time())


def build_claim_link(bot_username: str, sub_id: str, secret: str, *,
                     ttl: int = CLAIM_TTL_SECONDS) -> str:
    return f"https://t.me/{bot_username}?start={CLAIM_PREFIX}{claim_token(sub_id, secret, ttl=ttl)}"


async def find_claimable(cfg: Config, db: Database, *, sub_id: Optional[str] = None,
                         email: Optional[str] = None, token: Optional[str] = None) -> Optional[ClaimedClient]:
    """Ищет клиента панели по subId/email или по валидному токену привязки."""
    panel = get_panel(cfg, db)
    if not getattr(panel, "supports_claim", False):
        return None
    clients = await panel.list_claimable()
    if not clients:
        return None
    if token is not None:
        parsed = parse_token(token)
        if parsed is None:
            return None
        exp, sig = parsed
        if exp <= int(time.time()):
            return None
        for client in clients:
            if hmac.compare_digest(sig, _signature(client.sub_id, exp, cfg.claim_secret)):
                return client
        return None
    key = (sub_id or email or "").strip()
    if not key:
        return None
    for client in clients:
        if client.sub_id == key or client.email == key:
            return client
    return None


@dataclass
class ClaimResult:
    ok: bool
    message: str
    sub_row: object = None


async def bind_claim(bot: Bot, cfg: Config, db: Database, user_id: int, token: str) -> ClaimResult:
    """Привязывает клиента панели (по токену из deep-link) к Telegram-пользователю."""
    if not cfg.claim_enabled or not cfg.claim_secret:
        return ClaimResult(False, "Привязка подписок отключена.")

    panel = get_panel(cfg, db)
    if not getattr(panel, "supports_claim", False):
        return ClaimResult(False, "Привязка по ссылке доступна только для панели 3x-ui.")

    client = await find_claimable(cfg, db, token=token)
    if client is None:
        if parse_token(token) is None:
            message = "Ссылка повреждена или недействительна."
        elif token_expired(token):
            message = "Срок действия ссылки истёк — попросите новую."
        else:
            message = "Подписка не найдена. Проверьте ссылку или обратитесь в поддержку."
        return ClaimResult(False, message)

    existing = await db.subscription_by_panel_username("xui", client.email)
    if existing is not None:
        if int(existing["user_id"]) == user_id:
            row = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (int(existing["id"]),))
            await send_subscription_card(bot, cfg, db, user_id, row, with_qr=True)
            return ClaimResult(True, "Эта подписка уже привязана к вам 👇", row)
        return ClaimResult(False, "Эта подписка уже привязана к другому аккаунту.")

    if client.expires_at is None:
        return ClaimResult(False, "У клиента не задан срок в панели — обратитесь в поддержку.")
    if client.expires_at <= utcnow():
        return ClaimResult(False, "Срок действия в панели истёк — сначала продлите его, затем привязывайте.")
    if not client.enabled:
        return ClaimResult(False, "Клиент в панели отключён — включите его и повторите.")

    if await db.active_subscription(user_id) is not None:
        return ClaimResult(False, "У вас уже есть активная подписка — привязать ещё одну нельзя.")

    sub_id = await db.claim_subscription(
        user_id=user_id, plan_code="panel", vpn_key=client.vpn_key, sub_url=client.sub_url,
        panel="xui", panel_username=client.email, devices=client.devices,
        traffic_gb=client.traffic_gb, expires_at=client.expires_at,
    )
    if not sub_id:
        # Гонка: клиента привязал кто-то параллельно.
        existing = await db.subscription_by_panel_username("xui", client.email)
        if existing is not None and int(existing["user_id"]) == user_id:
            row = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (int(existing["id"]),))
            await send_subscription_card(bot, cfg, db, user_id, row, with_qr=True)
            return ClaimResult(True, "Эта подписка уже привязана к вам 👇", row)
        return ClaimResult(False, "Эта подписка уже привязана к другому аккаунту.")

    row = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    await send_subscription_card(bot, cfg, db, user_id, row, with_qr=True)
    await notify_admins(
        bot, cfg,
        f"🔗 <b>Клиент панели привязан</b>\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Клиент: <code>{esc(client.email)}</code>\n"
        f"До: {fmt_dt(client.expires_at)}",
    )
    return ClaimResult(True, "Подписка привязана 👇", row)


__all__ = [
    "CLAIM_PREFIX", "CLAIM_TTL_SECONDS", "claim_token", "parse_token", "token_expired",
    "build_claim_link", "find_claimable", "bind_claim", "ClaimResult",
]
