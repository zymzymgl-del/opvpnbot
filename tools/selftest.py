#!/usr/bin/env python3
"""Автономная проверка логики бота без сети и Telegram.

Запуск:  python tools/selftest.py

Проверяет: инициализацию БД, выдачу ключей через панель (заглушка), продление, оплату заказа,
промокоды, напоминания, отключение истёкших подписок, сборку ссылок 3x-ui
и валидацию конфигурации.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sqlite3
import ssl
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="opvpnbot-selftest-")
os.environ.update({
    "BOT_TOKEN": "123456:TEST-TOKEN",
    "ADMIN_IDS": "777",
    "DB_PATH": str(Path(TMP) / "test.sqlite3"),
    "VPN_PANEL": "xui",
    "XUI_URL": "http://127.0.0.1:2053",
    "XUI_API_TOKEN": "test-token",
    "PAYMENT_METHODS": "yookassa",
    # Ключи задаём заранее: validate(cfg) требует их при включённом yookassa,
    # а тест клиента ЮKassa (раздел 10) всё равно перезапишет эти переменные.
    "YOOKASSA_SHOP_ID": "123456",
    "YOOKASSA_SECRET_KEY": "test_secret_key",
    "YOOKASSA_RETURN_URL": "https://t.me/test_bot",
    "TRIAL_ENABLED": "true",
    "TRIAL_DAYS": "2",
    "LOG_FILE": str(Path(TMP) / "bot.log"),
})

from app import __build__  # noqa: E402
from app.config import load_config, validate  # noqa: E402
from app.db import Database, from_iso, to_iso, utcnow  # noqa: E402
from app.keyboards import admin_service_kb, main_menu, payment_methods_kb, plans_kb, profile_kb  # noqa: E402
from app.logctx import current_request_id, log_context  # noqa: E402
from app.runtime import runtime as rt  # noqa: E402
from app.services.panels import (  # noqa: E402
    BasePanel,
    ClaimedClient,
    IssuedKey,
    PanelError,
    XuiPanel,
    _as_dict,
    _build_link,
    _flow_applies_to_all,
    get_panel,
)
from app.services import panels as panels_mod  # noqa: E402
from app.services.claims import bind_claim, build_claim_link, claim_token, parse_token, token_expired  # noqa: E402
from app.services.subs import (  # noqa: E402
    complete_order,
    grant_subscription,
    process_expired,
    reissue_subscription,
    renewal_available,
    send_reminders,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  ❌ {name} — {detail}")


class FakeBot:
    """Заглушка aiogram.Bot: запоминает исходящие сообщения."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[int] = []
        self.markups: list[tuple[int, object]] = []
        self._next_id = 1000

    def _msg(self):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_message(self, chat_id, text, reply_markup=None, disable_web_page_preview=None, **kw):
        self.messages.append((int(chat_id), text))
        self.markups.append((int(chat_id), reply_markup))
        return self._msg()

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, **kw):
        self.messages.append((int(chat_id), text))
        self.markups.append((int(chat_id), reply_markup))
        return self._msg()

    async def delete_message(self, chat_id, message_id=None, **kw):
        return True

    async def send_photo(self, chat_id, photo=None, caption=None, reply_markup=None, **kw):
        self.photos.append(int(chat_id))
        return self._msg()

    def last_text_for(self, chat_id: int) -> str:
        for cid, text in reversed(self.messages):
            if cid == chat_id:
                return text
        return ""

    def all_text_for(self, chat_id: int) -> str:
        return "\n".join(text for cid, text in self.messages if cid == chat_id)

    def last_markup_for(self, chat_id: int):
        for cid, markup in reversed(self.markups):
            if cid == chat_id:
                return markup
        return None


class FakePanel(BasePanel):
    """Заглушка панели 3x-ui: выдаёт синтетические ключи без сети."""

    name = "xui"
    supports_claim = False

    def __init__(self) -> None:
        self.counter = 0
        self.fail_extend = False
        self.fail_create = False
        self.deleted: list[str] = []
        self.last_days = 0

    async def create_client(self, *, user_id: int, days: int, devices: int, traffic_gb: int,
                            plan_code: str) -> IssuedKey:
        if self.fail_create:
            raise PanelError("панель недоступна (заглушка)")
        self.counter += 1
        self.last_days = days
        return IssuedKey(
            vpn_key=f"vless://fake-{user_id}-{self.counter}@example.com:443#k",
            sub_url=None,
            panel=self.name,
            panel_username=f"tg{user_id}",
        )

    async def extend_client(self, *, panel_username: str, days: int,
                            devices: int | None = None, traffic_gb: int | None = None) -> bool:
        return not self.fail_extend

    async def set_enabled(self, *, panel_username: str, enabled: bool) -> bool:
        return True

    async def delete_client(self, *, panel_username: str) -> bool:
        self.deleted.append(panel_username)
        return True

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


async def run() -> None:
    cfg = load_config()
    print("\n1. Конфигурация и БД")
    problems = validate(cfg)
    check("конфиг валиден", problems == [], "; ".join(problems))
    check("версия сборки определена", bool(__build__) and " " not in __build__, __build__)
    db = Database(cfg.db_path)
    await db.connect()
    await rt.load(db, cfg)
    fake_panel = FakePanel()
    panels_mod._panel = fake_panel  # type: ignore[attr-defined]
    check("тарифы посеяны по умолчанию", len(await db.active_plans()) == 4)

    print("\n1b. 3x-ui: проверка TLS-сертификата")
    check("TLS-проверка включена по умолчанию", isinstance(XuiPanel(cfg).ssl_context, ssl.SSLContext))
    check("XUI_VERIFY_SSL=false отключает проверку",
          XuiPanel(dataclasses.replace(cfg, xui_verify_ssl=False)).ssl_context is False)

    print("\n1c. Контекст логирования (request_id)")
    with log_context("test-rid") as rid:
        check("request_id установлен внутри контекста",
              rid == "test-rid" and current_request_id() == "test-rid")
    check("request_id сброшен после выхода из контекста", current_request_id() == "-")

    print("\n1d. Обслуживание: клавиатура и панель")
    labels = {b.text for row in admin_service_kb().inline_keyboard for b in row}
    check("кнопки обслуживания: перезапуск бота и Xray",
          {"🔄 Перезапустить бота", "♻️ Перезапустить Xray"} <= labels, str(labels))
    check("XuiPanel умеет перезапускать Xray", callable(getattr(XuiPanel, "restart_xray", None)))

    # Тест ниже хардкодом использует коды "1m"/"3m", а DEFAULT_PLANS в db.py — "2devic"/"3devic"/…
    # без этих тарифов сценарий падает на первом же grant_subscription.
    await db.upsert_plan("1m", "1 месяц", 150, 30, 0, 2)
    await db.upsert_plan("3m", "3 месяца", 405, 90, 0, 3)

    bot = FakeBot()
    print("\n2. Выдача и продление подписок (панель 3x-ui, заглушка)")
    await db.ensure_user(1001, "alice", "Алиса")
    await db.ensure_user(1002, "bob", "Боб")
    sub = await grant_subscription(bot, cfg, db, 1001, "1m")
    check("подписка создана", sub is not None and sub["status"] == "active")
    check("ключ выдан панелью", (sub["vpn_key"] or "").startswith("vless://fake-"), sub["vpn_key"] or "")
    check("панель подписки — xui", sub["panel"] == "xui", str(sub["panel"]))
    check("срок = 30 дней", 29 <= (from_iso(sub["expires_at"]) - utcnow()).days + 1 <= 30)
    check("пользователю отправлена карточка", "VPN готов" in bot.all_text_for(1001))
    check("QR отдельным сообщением не отправляется", 1001 not in bot.photos)

    extended = await grant_subscription(bot, cfg, db, 1001, "1m")
    delta_days = (from_iso(extended["expires_at"]) - from_iso(sub["expires_at"])).days
    check("продление сдвинуло срок на 30 дней", delta_days == 30, str(delta_days))
    renew_markup = bot.last_markup_for(1001)
    renew_buttons = ({b.text for row in renew_markup.inline_keyboard for b in row}
                     if renew_markup is not None else set())
    check("продление: есть кнопки «Моя подписка» и «В меню»",
          {"🔑 Моя подписка", "🏠 В меню"} <= renew_buttons, str(renew_buttons))
    check("ключ при продлении не изменился", extended["vpn_key"] == sub["vpn_key"])
    check("клиент панели при продлении тот же", extended["panel_username"] == sub["panel_username"])

    print("\n2b. Возобновление после истечения (тот же клиент)")
    await db.update_subscription(int(sub["id"]), status="expired",
                                 expires_at=to_iso(utcnow() - timedelta(days=10)))
    revived = await grant_subscription(bot, cfg, db, 1001, "3m")
    check("истёкшая подписка переиспользует того же клиента",
          int(revived["id"]) == int(sub["id"]), str(revived["id"]))
    check("конфиг не изменился", revived["vpn_key"] == sub["vpn_key"])
    check("подписка снова активна", revived["status"] == "active", str(revived["status"]))
    check("срок считается от текущего момента",
          (from_iso(revived["expires_at"]) - utcnow()).days >= 88,
          str((from_iso(revived["expires_at"]) - utcnow()).days))
    check("новых подписок не появилось", len(await db.user_subscriptions(1001)) == 1)

    print("\n2c. Перевыдача вместо второй подписки (клиент панели сломан)")
    await db.ensure_user(1004, "dave", "Дейв")
    first = await grant_subscription(bot, cfg, db, 1004, "1m")
    fake_panel.fail_extend = True
    reissued = await grant_subscription(bot, cfg, db, 1004, "1m")
    fake_panel.fail_extend = False
    user_subs = await db.user_subscriptions(1004)
    check("перевыдача: вторая подписка не создана", len(user_subs) == 1, str(len(user_subs)))
    check("перевыдача обновила ту же строку", int(reissued["id"]) == int(first["id"]),
          f"{reissued['id']} != {first['id']}")
    check("перевыдача: активная ровно одна",
          await db.scalar("SELECT COUNT(*) FROM subscriptions WHERE user_id = 1004 AND status = 'active'") == 1)
    check("перевыдача выдала новый конфиг", reissued["vpn_key"] != first["vpn_key"], reissued["vpn_key"] or "")
    check("перевыдача: подписка активна", reissued["status"] == "active", str(reissued["status"]))
    check("перевыдача: пользователю сказали о перевыдаче", "перевыдан" in bot.all_text_for(1004))
    dup = await db.create_subscription(
        user_id=1004, plan_code="1m", vpn_key="vless://dup", sub_url=None, panel="xui",
        panel_username="dup", devices=1, traffic_gb=0, expires_at=utcnow() + timedelta(days=30),
    )
    check("вторую активную подписку блокирует БД", dup == 0, str(dup))

    print("\n2d. Ручная перевыдача админом (срок и тариф сохраняются)")
    await db.ensure_user(1005, "eve", "Ева")
    evo_sub = await grant_subscription(bot, cfg, db, 1005, "3m")
    # Оставим ровно 40 дней: перевыдача должна сохранить срок, а не сбросить его.
    keep_expires = utcnow() + timedelta(days=40)
    await db.update_subscription(int(evo_sub["id"]), expires_at=to_iso(keep_expires),
                                 devices=3, traffic_gb=50)
    evo_sub = await db.get_subscription(int(evo_sub["id"]))
    before_msgs = len(bot.messages)
    res = await reissue_subscription(bot, cfg, db, int(evo_sub["id"]), actor_id=777)
    check("ручная перевыдача: ok", res["ok"], res.get("error", ""))
    new_row = await db.get_subscription(int(evo_sub["id"]))
    check("перевыдача: id подписки тот же (нет второй)", int(new_row["id"]) == int(evo_sub["id"]))
    check("перевыдача: срок сохранён точно", new_row["expires_at"] == evo_sub["expires_at"],
          f"{new_row['expires_at']} != {evo_sub['expires_at']}")
    check("перевыдача: тариф сохранён", new_row["plan_code"] == evo_sub["plan_code"])
    check("перевыдача: устройства/трафик сохранены",
          int(new_row["devices"]) == 3 and int(new_row["traffic_gb"]) == 50,
          f"{new_row['devices']}/{new_row['traffic_gb']}")
    check("перевыдача: ссылка заменена новой", new_row["vpn_key"] != evo_sub["vpn_key"],
          new_row["vpn_key"] or "")
    check("перевыдача: клиент панели создаётся на остаток дней", fake_panel.last_days == 40,
          str(fake_panel.last_days))
    check("перевыдача: старый клиент панели удалён", evo_sub["panel_username"] in fake_panel.deleted,
          str(fake_panel.deleted))
    user_texts = "\n".join(t for _c, t in bot.messages[before_msgs:])
    check("перевыдача: пользователь получил уведомление о новой ссылке",
          "перевыдана вручную" in user_texts and "Новая ссылка" in user_texts)
    admin_texts = "\n".join(t for c, t in bot.messages[before_msgs:] if c == 777)
    check("перевыдача: админы уведомлены", "Ручная перевыдача" in admin_texts)

    fake_panel.fail_create = True
    res_fail = await reissue_subscription(bot, cfg, db, int(evo_sub["id"]))
    fake_panel.fail_create = False
    check("перевыдача: отказ панели обработан", not res_fail["ok"] and "Панель отказала" in res_fail["error"])
    row_after_fail = await db.get_subscription(int(evo_sub["id"]))
    check("перевыдача: при отказе панели ссылка не тронута",
          row_after_fail["vpn_key"] == new_row["vpn_key"])

    print("\n3. Пробный период")
    trial = await grant_subscription(bot, cfg, db, 1002, "trial")
    check("пробная подписка выдана", trial is not None and trial["panel"] == "xui")
    trial_days = (from_iso(trial["expires_at"]) - utcnow()).days
    check("пробный срок из runtime (2 дня)", trial_days in (1, 2), str(trial_days))
    upgrade = await grant_subscription(bot, cfg, db, 1002, "1m")
    check("продление подняло лимит устройств до тарифа (1→2)", int(upgrade["devices"]) == 2, str(upgrade["devices"]))
    check("продление сохранило безлимит по трафику (0)", int(upgrade["traffic_gb"]) == 0, str(upgrade["traffic_gb"]))
    up_3 = await grant_subscription(bot, cfg, db, 1002, "3m")
    check("тариф на 3 устройства поднял лимит (2→3)", int(up_3["devices"]) == 3, str(up_3["devices"]))
    down_2 = await grant_subscription(bot, cfg, db, 1002, "1m")
    check("тариф на 2 устройства понизил лимит (3→2)", int(down_2["devices"]) == 2, str(down_2["devices"]))

    print("\n3b. Триал: атомарный захват (защита от двойной выдачи)")
    await db.ensure_user(1003, "carol", "Кэрол")
    check("первый захват триала успешен", await db.claim_trial(1003) is True)
    check("повторный захват триала отклонён", await db.claim_trial(1003) is False)
    check("флаг trial_used выставлен", int((await db.get_user(1003))["trial_used"]) == 1)
    await db.set_user_field(1003, "trial_used", 0)
    check("после возврата права триал снова доступен", await db.claim_trial(1003) is True)

    print("\n3.5 Окно продления (за notify_days_before до конца)")
    fresh = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (int(sub["id"]),))
    check("продление вне окна (30 дн.) недоступно", renewal_available(fresh, cfg) is False)
    await db.update_subscription(int(sub["id"]), expires_at=to_iso(utcnow() + timedelta(days=2)))
    soon = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (int(sub["id"]),))
    check("продление в окне (2 дн.) доступно", renewal_available(soon, cfg) is True)

    print("\n4. Заказы и оплата")
    await db.ensure_user(3001, "buyer", "Покупатель")
    order_id = await db.create_order(3001, "1m", 299, 299, None, "yookassa")
    await complete_order(bot, cfg, db, order_id, payment_id="star-charge-1")
    order = await db.get_order(order_id)
    check("заказ помечен оплаченным", order["status"] == "paid")
    buyer_sub = await db.active_subscription(3001)
    check("подписка выдана по заказу", buyer_sub is not None)

    # История счетов заказа: даже «затёртый» счёт должен находить заказ
    await db.ensure_user(3002, "buyer2", "Покупатель 2")
    order2 = await db.create_order(3002, "1m", 299, 299, None, "none")
    await db.set_order_payment(order2, "pay-first", method="yookassa")
    await db.set_order_payment(order2, "pay-second", method="yookassa")
    ids = (await db.get_order(order2))["payment_ids"]
    check("история счетов хранит оба счёта", ids == "pay-first,pay-second", str(ids))
    found_first = await db.order_by_payment("pay-first")
    check("заказ находится по первому (затёртому) счёту",
          found_first is not None and int(found_first["id"]) == order2)
    any_pending = await db.user_pending_order(3002)
    check("незакрытый заказ находится без привязки к тарифу",
          any_pending is not None and int(any_pending["id"]) == order2)

    # Смена тарифа в том же заказе (режим «одно окно»): пересчёт суммы со скидкой
    await db.update_order_plan(order2, "3m", 405, 101, "SALE25", 1)
    switched = await db.get_order(order2)
    check("смена тарифа обновила тариф/сумму с пересчётом скидки",
          switched["plan_code"] == "3m" and int(switched["base_amount"]) == 405
          and int(switched["amount"]) == 405 - 101 and int(switched["promo_reserved"]) == 1,
          f"plan={switched['plan_code']}, base={switched['base_amount']}, "
          f"amount={switched['amount']}")

    print("\n5. Промокоды")
    await db.create_promo("vpn10", "percent", 10, max_uses=1, expires_at=None)
    ok, reason, promo = await db.promo_valid("VPN10", 3001)
    check("промокод валиден для нового пользователя", ok, reason)
    await db.apply_promo("VPN10", 3001)
    ok2, reason2, _ = await db.promo_valid("VPN10", 3001)
    check("повторное использование запрещено", not ok2, reason2)
    ok3, reason3, _ = await db.promo_valid("VPN10", 4001)
    check("лимит активаций соблюдается", not ok3, reason3)

    base, discount = 1000, 0
    promo_row = await db.get_promo("VPN10")
    discount = base * int(promo_row["value"]) // 100
    check("расчёт скидки 10% корректен", discount == 100, str(discount))

    print("\n6. Напоминания и истечение подписок")
    await db.update_subscription(int(buyer_sub["id"]), expires_at=to_iso(utcnow() + timedelta(hours=20)))
    await send_reminders(bot, cfg, db)
    check("напоминание за 1 день отправлено", "заканчивается" in bot.last_text_for(3001))
    await db.update_subscription(int(buyer_sub["id"]), expires_at=to_iso(utcnow() - timedelta(minutes=5)))
    await process_expired(bot, cfg, db)
    refreshed = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (int(buyer_sub["id"]),))
    check("истёкшая подписка помечена expired", refreshed["status"] == "expired", refreshed["status"])
    check("пользователь уведомлён об окончании", "закончилась" in bot.last_text_for(3001))

    print("\n7. Сборка ссылок 3x-ui")
    vless_inbound = {
        "id": 1, "protocol": "vless", "port": 443,
        "settings": '{"clients": [{"id": "uuid-1", "flow": "xtls-rprx-vision", "email": "tg1_1m_x"}]}',
        "streamSettings": (
            '{"network": "tcp", "security": "reality", "realitySettings": {"serverNames": ["www.microsoft.com"], '
            '"shortIds": ["abcd1234"], "settings": {"publicKey": "PUBKEY123", "fingerprint": "chrome"}}}'
        ),
    }
    link = _build_link(vless_inbound, {"id": "uuid-1", "flow": "xtls-rprx-vision", "email": "tg1_1m_x"}, "1.2.3.4")
    check("vless+reality ссылка собрана", link.startswith("vless://uuid-1@1.2.3.4:443?"), link)
    check("в ссылке есть pbk/sni/sid", all(p in link for p in ("pbk=PUBKEY123", "sni=www.microsoft.com", "sid=abcd1234")))

    ws_inbound = {
        "id": 2, "protocol": "vless", "port": 8443,
        "settings": '{"clients": [{"id": "uuid-2", "email": "u2"}]}',
        "streamSettings": (
            '{"network": "ws", "security": "tls", "tlsSettings": {"serverName": "cdn.example.com", "alpn": ["h2"]},'
            ' "wsSettings": {"path": "/secret", "headers": {"Host": "cdn.example.com"}}}'
        ),
    }
    ws_link = _build_link(ws_inbound, {"id": "uuid-2", "email": "u2"}, "example.com")
    check("vless+ws+tls ссылка собрана", "type=ws" in ws_link and "path=%2Fsecret" in ws_link, ws_link)

    vmess_inbound = {
        "id": 3, "protocol": "vmess", "port": 8080,
        "settings": '{"clients": [{"id": "uuid-3", "alterId": 0, "email": "u3"}]}',
        "streamSettings": '{"network": "tcp", "security": "none"}',
    }
    vmess_link = _build_link(vmess_inbound, {"id": "uuid-3", "alterId": 0, "email": "u3"}, "example.com")
    check("vmess ссылка (base64) собрана", vmess_link.startswith("vmess://"), vmess_link[:20])

    trojan_inbound = {
        "id": 4, "protocol": "trojan", "port": 443,
        "settings": '{"clients": [{"id": "pass123", "email": "u4"}]}',
        "streamSettings": '{"network": "tcp", "security": "tls", "tlsSettings": {"serverName": "t.example.com"}}',
    }
    trojan_link = _build_link(trojan_inbound, {"id": "pass123", "email": "u4"}, "example.com")
    check("trojan ссылка собрана", trojan_link.startswith("trojan://pass123@"), trojan_link)

    print("\n8. Инициализация панелей")
    os.environ["XUI_URL"] = "http://127.0.0.1:2053/secretpath"
    os.environ["XUI_USERNAME"] = "admin"
    os.environ["XUI_PASSWORD"] = "pass"
    xui_cfg = load_config()
    XuiPanel(xui_cfg)
    check("XUI URL разобран", XuiPanel(xui_cfg).base.endswith("/secretpath"))
    os.environ["XUI_INBOUND_IDS"] = "1, 3;7"
    multi_cfg = load_config()
    check("XUI_INBOUND_IDS разобран в список", multi_cfg.xui_inbound_ids == (1, 3, 7), str(multi_cfg.xui_inbound_ids))
    check("XuiPanel хранит список inbound'ов", XuiPanel(multi_cfg).inbound_ids == (1, 3, 7))
    os.environ.pop("XUI_INBOUND_IDS", None)
    check("мульти-inbound: flow корректен для vless tcp+tls/reality", _flow_applies_to_all([
        {"protocol": "vless", "streamSettings": '{"network": "tcp", "security": "reality"}'},
        {"protocol": "vless", "streamSettings": '{"network": "tcp", "security": "tls"}'},
    ]))
    check("мульти-inbound: ws/trojan отключают flow", not _flow_applies_to_all([
        {"protocol": "vless", "streamSettings": '{"network": "ws", "security": "tls"}'},
        {"protocol": "trojan", "streamSettings": '{"network": "tcp", "security": "tls"}'},
    ]))
    panel = get_panel(cfg, db)
    check("фабрика возвращает панель 3x-ui (заглушка)", panel is fake_panel)

    print("\n8b. Совместимость форм inbound (классика/новая схема clients)")
    check("_as_dict читает JSON-строку streamSettings", _as_dict('{"security": "reality"}') == {"security": "reality"})
    check("_as_dict принимает вложенный объект", _as_dict({"network": "tcp"}) == {"network": "tcp"})
    check("_as_dict устойчив к мусору", _as_dict("не json") == {} and _as_dict(None) == {})
    up = XuiPanel._update_payload({"uuid": "u-1", "email": "e", "allowedIPs": "", "limitIp": 2, "totalGB": 100})
    check("update-тело: id = uuid (не числовой id)", up["id"] == "u-1", str(up.get("id")))
    check("update-тело: allowedIPs приводится к массиву", up["allowedIPs"] == [])
    check("update-тело: прочие поля сохраняются", up["limitIp"] == 2 and up["totalGB"] == 100)

    print("\n9. Клавиатуры и статистика")
    check("главное меню собирается", main_menu(True, True) is not None)
    check("список тарифов собирается", plans_kb(await db.active_plans()) is not None)
    check("выбор способа оплаты собирается", payment_methods_kb(["yookassa"], 1) is not None)
    stats = await db.stats()
    check("статистика считается", stats["users_total"] >= 3 and stats["revenue_total"] >= 299, str(stats["revenue_total"]))


    print("\n10. Клиент ЮKassa (HTTP подменён заглушкой)")
    import json as _json

    from app.services.payments import YooKassaClient

    class FakeResponse:
        def __init__(self, status: int, payload: dict) -> None:
            self.status = status
            self._payload = payload

        async def text(self) -> str:
            return _json.dumps(self._payload)

        async def json(self, content_type=None) -> dict:
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, responses: list) -> None:
            self.responses = responses
            self.calls: list = []
            self.closed = False

        def post(self, url, json=None, headers=None, **kwargs):
            self.calls.append(("POST", url, json, headers))
            status, payload = self.responses.pop(0)
            return FakeResponse(status, payload)

        def get(self, url, params=None, **kwargs):
            self.calls.append(("GET", url, params, None))
            status, payload = self.responses.pop(0)
            return FakeResponse(status, payload)

    os.environ["YOOKASSA_SHOP_ID"] = "123456"
    os.environ["YOOKASSA_SECRET_KEY"] = "test_secret_key"
    os.environ["YOOKASSA_RETURN_URL"] = "https://t.me/test_bot"
    yk_cfg = load_config()
    client = YooKassaClient(yk_cfg)

    session = FakeSession([(200, {"id": "pay-1", "status": "pending",
                                  "confirmation": {"confirmation_url": "https://yookassa.ru/checkout/1"}})])
    async def fake_session():
        return session
    client._get_session = fake_session  # type: ignore[assignment]

    payment = await client.create_payment(amount_rub=150, description="VPN 1m заказ #1",
                                          metadata={"order_id": 1, "user_id": 555})
    method, url, payload, headers = session.calls[0]
    check("платёж создан через API v3", url.endswith("/v3/payments") and method == "POST", url)
    check("сумма передана в формате 150.00", payload["amount"]["value"] == "150.00", str(payload["amount"]))
    check("валюта RUB и capture=true", payload["amount"]["currency"] == "RUB" and payload["capture"] is True)
    check("return_url из конфига", payload["confirmation"]["return_url"] == "https://t.me/test_bot")
    check("metadata содержит заказ", payload["metadata"]["order_id"] == "1", str(payload["metadata"]))
    check("Idempotence-Key передан", bool(headers.get("Idempotence-Key")), str(headers))
    check("confirmation_url получен", payment["confirmation"]["confirmation_url"].startswith("https://yookassa.ru"))

    session.responses.append((200, {"status": "succeeded", "paid": True, "amount": {"value": "150.00"}}))
    check("успешный платёж подтверждён", await client.is_paid("pay-1", 150) is True)

    session.responses.append((200, {"status": "succeeded", "paid": True, "amount": {"value": "100.00"}}))
    check("платёж с чужой суммой отклонён", await client.is_paid("pay-1", 150) is False)

    session.responses.append((200, {"status": "pending", "paid": False, "amount": {"value": "299.00"}}))
    check("неоплаченный платёж отклонён", await client.is_paid("pay-1", 150) is False)

    session.responses.append((400, {"description": "bad request"}))
    check("ошибка API обрабатывается", await client.is_paid("pay-x", 150) is False)

    print("\n11. Вебхук ЮKassa: разбор уведомления")
    from app.webhook import YOOKASSA_NETS, _ip_allowed, _order_by_metadata
    check("IP ЮKassa из белого списка разрешён", _ip_allowed("185.71.76.10"))
    check("посторонний IP отклонён", not _ip_allowed("8.8.8.8"))
    check("диапазоны IP заданы", len(YOOKASSA_NETS) >= 6)
    recovered = await _order_by_metadata(db, {"metadata": {"order_id": str(order2)}})
    check("заказ восстанавливается по metadata.order_id платежа",
          recovered is not None and int(recovered["id"]) == order2)
    check("платёж без metadata не роняет восстановление",
          await _order_by_metadata(db, {}) is None)


    check("профиль содержит кнопку истории заказов", "orders:history" in profile_kb().inline_keyboard.__repr__())
    check("в профиле нет кнопок баланса/рефералки",
          "topup:start" not in profile_kb().inline_keyboard.__repr__()
          and "ref:open" not in profile_kb().inline_keyboard.__repr__())

    print("\n12. Конкурентность и идемпотентность выдачи")
    # 12a. Двойное завершение одного заказа: только один вызов "захватывает" pending -> paid
    await db.ensure_user(5001, "carol", "Кэрол")
    race_order = await db.create_order(5001, "1m", 150, 150, None, "yookassa", "subscription")
    results = await asyncio.gather(*(complete_order(bot, cfg, db, race_order) for _ in range(5)))
    order_5001 = await db.get_order(race_order)
    subs_5001 = await db.fetchall("SELECT * FROM subscriptions WHERE user_id = 5001")
    pays_5001 = await db.fetchall("SELECT * FROM payments_log WHERE user_id = 5001 AND kind = 'order'")
    check("все вызовы complete_order вернули успех (идемпотентно)", all(results), str(results))
    check("заказ перешёл в paid ровно один раз (статус paid)", order_5001["status"] == "paid")
    check("подписка выдана ровно один раз", len(subs_5001) == 1, str(len(subs_5001)))
    check("платёж зафиксирован ровно один раз", len(pays_5001) == 1, str(len(pays_5001)))

    # 12b. Промокод: лимит max_uses не превышается при параллельной активации
    await db.create_promo("RACE50", "fixed", 50, max_uses=2, expires_at=None)
    for uid, name in ((5005, "gina"), (5006, "hugo"), (5007, "ivan")):
        await db.ensure_user(uid, name, name)
    reserved = await asyncio.gather(
        db.reserve_promo("RACE50", 5005),
        db.reserve_promo("RACE50", 5006),
        db.reserve_promo("RACE50", 5007),
    )
    ok_count = sum(1 for ok, _r, _row in reserved if ok)
    used_total = int(await db.scalar("SELECT used FROM promos WHERE code = 'RACE50'"))
    uses_rows = int(await db.scalar("SELECT COUNT(*) FROM promo_uses WHERE code = 'RACE50'"))
    check("промокод: лимит max_uses=2 соблюдён при параллельной активации",
          ok_count == 2 and used_total == 2 and uses_rows == 2,
          f"ok={ok_count}, used={used_total}, rows={uses_rows}")

    # 12c. Промокод: повторная активация тем же пользователем (даже параллельно) отклонена
    winner = next(uid for uid, (ok, _r, _row) in zip((5005, 5006, 5007), reserved) if ok)
    twice = await asyncio.gather(db.reserve_promo("RACE50", winner), db.reserve_promo("RACE50", winner))
    check("промокод: повторная активация тем же пользователем отклонена",
          not any(ok for ok, _r, _row in twice))

    # 12d. release_promo освобождает лимит для повторного использования
    await db.release_promo("RACE50", winner)
    used_after_release = int(await db.scalar("SELECT used FROM promos WHERE code = 'RACE50'"))
    check("промокод: release уменьшает счётчик", used_after_release == 1, str(used_after_release))
    await db.ensure_user(5008, "jack", "Джек")
    ok_refill, _r, _row = await db.reserve_promo("RACE50", 5008)
    check("промокод: освобождённое место переиспользуется", ok_refill)

    print("\n13. Безопасность вебхука и автоматические бэкапы")
    # 13a. Фильтр IP webhook-сервера
    from app.webhook import _client_ip, _ip_allowed

    check("IP из диапазона ЮKassa разрешён", _ip_allowed("185.71.76.5"))
    check("IP из диапазона ЮKassa (IPv6) разрешён", _ip_allowed("2a02:5180::1"))
    check("посторонний IP отклоняется", not _ip_allowed("8.8.8.8"))
    check("некорректный IP отклоняется", not _ip_allowed("not-an-ip"))

    class FakeReq:
        def __init__(self, remote, xff=None):
            self.remote = remote
            self.headers = {"X-Forwarded-For": xff} if xff else {}

    def _sqlite_backup_ok(p: Path) -> bool:
        if not p.exists():
            return False
        con = sqlite3.connect(str(p))
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"users", "orders", "subscriptions"} <= names
        finally:
            con.close()

    check("без доверия прокси берётся TCP-адрес, XFF игнорируется",
          _client_ip(FakeReq("10.0.0.1", "185.71.76.5"), trust_proxy=False) == "10.0.0.1")
    check("с доверием прокси берётся первый адрес из XFF",
          _client_ip(FakeReq("10.0.0.1", "185.71.76.5, 10.0.0.1"), trust_proxy=True) == "185.71.76.5")

    # 13b. Автоматический бэкап БД с ротацией
    from app.services.subs import run_db_backup

    import dataclasses as _dc

    backup_cfg = _dc.replace(cfg, backup_interval_minutes=60, backup_keep=2)
    p1 = await run_db_backup(backup_cfg, db)
    check("бэкап создан", p1 is not None and Path(p1).exists(), str(p1))
    check("повторный вызов в пределах интервала ничего не создаёт",
          await run_db_backup(backup_cfg, db) is None)
    # форсируем «протухание» интервала: сдвигаем mtime назад
    os.utime(p1, (time.time() - 7200, time.time() - 7200))
    made = [p1]
    for i in range(3):
        os.utime(made[-1], (time.time() - 7200, time.time() - 7200))
        time.sleep(1.05)  # уникальная метка имени (секунды)
        pn = await run_db_backup(backup_cfg, db)
        if pn:
            made.append(pn)
    bdir = Path(cfg.db_path).parent / "backups"
    remaining = sorted(bdir.glob("opvpnbot-*.sqlite3"))
    check("ротация держит не больше backup_keep копий", len(remaining) <= 2, str([p.name for p in remaining]))
    check("копия читается как SQLite и содержит таблицы", _sqlite_backup_ok(remaining[-1] if remaining else Path(p1)))

    # 13c. Вебхук Telegram: конфиг, путь и secret_token
    from app.config import _validate_tg_webhook
    from app.webhook import _secret_ok

    check("вебхук Telegram по умолчанию выключен", cfg.tg_webhook_enabled is False)
    check("общий HTTP-сервер нужен только при включённом вебхуке",
          cfg.any_webhook_enabled == (cfg.webhook_enabled or cfg.tg_webhook_enabled))
    check("TG_WEBHOOK_URL по умолчанию пуст", cfg.tg_webhook_url == "")
    check("fast-ack включён по умолчанию", cfg.tg_webhook_fast_ack is True)
    check("путь по умолчанию — /tg/webhook", cfg.tg_webhook_path == "/tg/webhook")

    tg_on = _dc.replace(cfg, tg_webhook_enabled=True,
                        tg_webhook_url="https://bot.example.com/tg/webhook",
                        tg_webhook_secret="a" * 32)
    check("корректный вебхук Telegram не даёт ошибок", _validate_tg_webhook(tg_on) == [],
          "; ".join(_validate_tg_webhook(tg_on)))
    check("путь слушается там же, куда указывает публичный URL",
          tg_on.tg_webhook_path == "/tg/webhook")
    check("незаданный TG_WEBHOOK_URL отклоняется",
          _validate_tg_webhook(_dc.replace(tg_on, tg_webhook_url="")) != [])
    check("http:// вместо https:// отклоняется",
          _validate_tg_webhook(_dc.replace(tg_on, tg_webhook_url="http://bot.example.com/tg/webhook")) != [])
    check("URL без пути отклоняется",
          _validate_tg_webhook(_dc.replace(tg_on, tg_webhook_url="https://bot.example.com")) != [])
    check("пустой secret отклоняется", _validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="")) != [])
    check("короткий secret отклоняется",
          _validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="short")) != [])
    check("secret с недопустимыми символами отклоняется",
          _validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="не-секрет-12345")) != [])
    check("совпадение путей с вебхуком ЮKassa отклоняется",
          any("один путь" in p for p in _validate_tg_webhook(_dc.replace(tg_on, webhook_path="/tg/webhook"))))
    check("секрет сверяется побайтово", _secret_ok("a" * 32, "a" * 32))
    check("неверный секрет отклоняется", not _secret_ok("b" * 32, "a" * 32))
    check("пустой ожидаемый секрет не пропускает запрос", not _secret_ok("", ""))

    print("\n14. Привязка существующего клиента панели (QR/claim)")

    class StubXuiPanel(BasePanel):
        name = "xui"
        supports_claim = True

        def __init__(self, client: ClaimedClient) -> None:
            self.client = client

        async def create_client(self, **kwargs):  # pragma: no cover - не используется
            raise NotImplementedError

        async def list_claimable(self):
            return [self.client]

    cfg_claim = dataclasses.replace(cfg, claim_enabled=True, claim_secret="selftest-secret")
    legacy = ClaimedClient(
        email="legacy_client", sub_id="SUBID-123", vpn_key="vless://legacy@example.com:443#legacy",
        sub_url=None, expires_at=utcnow() + timedelta(days=10), devices=2, traffic_gb=0, enabled=True,
    )
    saved_panel = panels_mod._panel
    panels_mod._panel = StubXuiPanel(legacy)  # type: ignore[attr-defined]
    try:
        token = claim_token("SUBID-123", "selftest-secret")
        link = build_claim_link("opvpnbot_test_bot", "SUBID-123", "selftest-secret")
        check("ссылка привязки ведёт на бота и подписана",
              link.startswith("https://t.me/opvpnbot_test_bot?start=claim_")
              and not token_expired(link.rsplit("claim_", 1)[1]))
        exp_custom, _sig = parse_token(claim_token("SUBID-123", "selftest-secret", ttl=3600))
        check("токен: срок задаётся ttl", abs(exp_custom - (int(time.time()) + 3600)) <= 5, str(exp_custom))
        tok = claim_token("SUBID-123", "selftest-secret")
        check("токен: только Telegram-допустимые символы (A-Za-z0-9_-)",
              all(c.isalnum() or c in "_-" for c in tok), tok)

        await db.ensure_user(6001, "claim1", "Клейм1")
        res = await bind_claim(bot, cfg_claim, db, 6001, token)
        check("привязка: успешно", res.ok, res.message)
        bound = await db.active_subscription(6001)
        check("привязка: подписка создана по клиенту панели",
              bound is not None and bound["panel_username"] == "legacy_client", str(bound and bound["panel_username"]))
        check("привязка: срок взят из панели",
              bound is not None and 9 <= (from_iso(bound["expires_at"]) - utcnow()).days <= 10)
        check("привязка: карточка отправлена", "VPN готов" in bot.all_text_for(6001))
        check("привязка: QR отправлен", 6001 in bot.photos)

        res_again = await bind_claim(bot, cfg_claim, db, 6001, token)
        count = int(await db.scalar("SELECT COUNT(*) FROM subscriptions WHERE panel_username = 'legacy_client'"))
        check("привязка: повтор тем же юзером идемпотентен", res_again.ok and count == 1, str(count))

        await db.ensure_user(6002, "claim2", "Клейм2")
        res_foreign = await bind_claim(bot, cfg_claim, db, 6002, token)
        check("привязка: чужой claim отклонён", not res_foreign.ok, res_foreign.message)

        res_bad = await bind_claim(bot, cfg_claim, db, 6003, "deadbeef")
        check("привязка: неизвестный токен отклонён", not res_bad.ok)

        res_off = await bind_claim(bot, dataclasses.replace(cfg_claim, claim_enabled=False), db, 6003, token)
        check("привязка: feature выключена — отказ", not res_off.ok)

        no_expiry = ClaimedClient(email="no_exp", sub_id="SUBID-NOEXP", vpn_key="vless://x",
                                  sub_url=None, expires_at=None, devices=1, traffic_gb=0, enabled=True)
        panels_mod._panel = StubXuiPanel(no_expiry)  # type: ignore[attr-defined]
        res_noexp = await bind_claim(bot, cfg_claim, db, 6003, claim_token("SUBID-NOEXP", "selftest-secret"))
        check("привязка: клиент без срока отклонён", not res_noexp.ok)

        second = ClaimedClient(email="legacy_client_2", sub_id="SUBID-456", vpn_key="vless://y",
                               sub_url=None, expires_at=utcnow() + timedelta(days=5), devices=1,
                               traffic_gb=0, enabled=True)
        panels_mod._panel = StubXuiPanel(second)  # type: ignore[attr-defined]
        res_second = await bind_claim(bot, cfg_claim, db, 6001, claim_token("SUBID-456", "selftest-secret"))
        check("привязка: вторая активная подписка запрещена", not res_second.ok, res_second.message)

        await db.ensure_user(6004, "claim4", "Клейм4")
        expired = claim_token("SUBID-123", "selftest-secret", ttl=-1)
        check("токен: истёкший помечается", token_expired(expired))
        res_expired = await bind_claim(bot, cfg_claim, db, 6004, expired)
        check("привязка: истёкшая ссылка отклонена", not res_expired.ok and "истёк" in res_expired.message,
              res_expired.message)

        await db.ensure_user(6005, "claim5", "Клейм5")
        disabled = ClaimedClient(email="disabled_client", sub_id="SUBID-DIS", vpn_key="vless://dis",
                                 sub_url=None, expires_at=utcnow() + timedelta(days=15), devices=1,
                                 traffic_gb=0, enabled=False)
        panels_mod._panel = StubXuiPanel(disabled)  # type: ignore[attr-defined]
        res_disabled = await bind_claim(bot, cfg_claim, db, 6005, claim_token("SUBID-DIS", "selftest-secret"))
        check("привязка: отключённый клиент отклонён", not res_disabled.ok and "отключ" in res_disabled.message,
              res_disabled.message)

        await db.ensure_user(7001, "race1", "Рейс1")
        await db.ensure_user(7002, "race2", "Рейс2")
        race_client = ClaimedClient(email="race_client", sub_id="SUBID-RACE", vpn_key="vless://race",
                                    sub_url=None, expires_at=utcnow() + timedelta(days=8), devices=1,
                                    traffic_gb=0, enabled=True)
        panels_mod._panel = StubXuiPanel(race_client)  # type: ignore[attr-defined]
        race_token = claim_token("SUBID-RACE", "selftest-secret")
        race = await asyncio.gather(
            bind_claim(bot, cfg_claim, db, 7001, race_token),
            bind_claim(bot, cfg_claim, db, 7002, race_token),
        )
        race_ok = sum(1 for r in race if r.ok)
        race_rows = int(await db.scalar("SELECT COUNT(*) FROM subscriptions WHERE panel_username = 'race_client'"))
        check("привязка: конкурентные claim создают ровно одну подписку",
              race_ok == 1 and race_rows == 1, f"ok={race_ok}, rows={race_rows}")
    finally:
        panels_mod._panel = saved_panel  # type: ignore[attr-defined]

    print("\n15. Один заказ — один счёт ЮKassa (защита от дублей оплаты)")

    from app.handlers import user as user_mod

    class FakeCall:
        def __init__(self, order_id: int, user_id: int) -> None:
            self.data = f"pay:yookassa:{order_id}"
            self.from_user = SimpleNamespace(id=user_id)
            self.answers: list[str] = []
            self.edits: list[tuple[str, object]] = []

            outer = self

            class _Msg:
                def __init__(self):
                    self.chat = SimpleNamespace(id=user_id)
                    self.message_id = 1

                async def edit_text(self, text, reply_markup=None):
                    outer.edits.append((text, reply_markup))

                async def answer(self, text, reply_markup=None, **kw):
                    outer.edits.append((text, reply_markup))
                    return SimpleNamespace(message_id=2, chat=self.chat)

            self.message = _Msg()

            class _UiBot:
                async def edit_message_text(_self, text, chat_id=None, message_id=None,
                                            reply_markup=None, **kw):
                    return await self.message.edit_text(text, reply_markup=reply_markup)

                async def send_message(_self, chat_id, text, reply_markup=None, **kw):
                    return await self.message.answer(text, reply_markup=reply_markup)

                async def delete_message(_self, chat_id, message_id=None, **kw):
                    return True

            self.bot = _UiBot()

        async def answer(self, text="", show_alert=False):
            self.answers.append(text)

        @property
        def checkout_url(self) -> str:
            for _text, kb in reversed(self.edits):
                if not kb:
                    continue
                for row in kb.inline_keyboard:
                    for btn in row:
                        u = getattr(btn, "url", None)
                        if u and "checkout" in u:
                            return u
            return ""

    class FakeYk:
        """Заглушка ЮKassa: считает созданные счета, эмулирует статусы."""

        def __init__(self) -> None:
            self.created = 0
            self.statuses: dict[str, str] = {}

        async def create_payment(self, *, amount_rub, description, metadata,
                                 idempotence_key=None, ttl_seconds=None, **kw):
            self.created += 1
            pid = f"fake-pay-{self.created}"
            self.statuses[pid] = "pending"
            return {"id": pid, "status": "pending",
                    "confirmation": {"confirmation_url": f"https://yookassa.ru/checkout/{pid}"}}

        async def get_payment(self, payment_id):
            st = self.statuses.get(payment_id, "canceled")
            paid = st == "succeeded"
            return {"id": payment_id, "status": st, "paid": paid,
                    "amount": {"value": "150.00", "currency": "RUB"},
                    "confirmation": {"confirmation_url": f"https://yookassa.ru/checkout/{payment_id}"}}

        payment_is_valid = staticmethod(YooKassaClient.payment_is_valid)
        payment_is_payable = staticmethod(YooKassaClient.payment_is_payable)
        payment_is_done = staticmethod(YooKassaClient.payment_is_done)

    fake_yk = FakeYk()
    await db.ensure_user(8001, "dup", "Дубль")
    dup_order = await db.create_order(8001, "1m", 150, 150, None, "none", "subscription")
    # Три последовательных нажатия «Перейти к оплате» + параллельная гонка кликов.
    # Идемпотентный ключ f"order-{id}-amt-{amount}" одинаков для всех попыток при
    # неизменной сумме, поэтому настоящий ЮKassa вернул бы тот же счёт; заглушка
    # считает вызовы — в идеале их быть не должно (записываем фактическое число).
    seq_calls = []
    for _ in range(3):
        c = FakeCall(dup_order, 8001)
        await user_mod.cb_pay(c, db, cfg, bot, yk=fake_yk)
        seq_calls.append(c)
    race_calls = [FakeCall(dup_order, 8001) for _ in range(4)]
    await asyncio.gather(*(user_mod.cb_pay(c, db, cfg, bot, yk=fake_yk) for c in race_calls))
    dup_row = await db.get_order(dup_order)
    check("первое нажатие создаёт ровно один счёт", fake_yk.created >= 1)
    check("последующие нажатия не плодят счета сверх одного на слот",
          fake_yk.created == 1, f"created={fake_yk.created}")
    check("заказу привязан ровно один платёж", bool(dup_row["payment_id"]))
    check("ссылка сохранена в заказе (payment_url)",
          dup_row["payment_url"] == f"https://yookassa.ru/checkout/{dup_row['payment_id']}",
          str(dup_row["payment_url"]))
    last = FakeCall(dup_order, 8001)
    created_before = fake_yk.created
    await user_mod.cb_pay(last, db, cfg, bot, yk=fake_yk)
    check("повторное открытие даёт ту же ссылку без нового счёта",
          fake_yk.created == created_before
          and last.checkout_url == dup_row["payment_url"],
          f"url={last.checkout_url!r}, stored={dup_row['payment_url']!r}")
    shown_urls = {c.checkout_url for c in [*seq_calls, *race_calls, last] if c.checkout_url}
    check("все окна оплаты показывают одну и ту же ссылку",
          shown_urls == {dup_row["payment_url"]}, str(shown_urls))
    # Платёж прошёл через «дублирующее» окно: complete_order идемпотентен
    fake_yk.statuses[dup_row["payment_id"]] = "succeeded"
    done_call = FakeCall(dup_order, 8001)
    await user_mod.cb_pay(done_call, db, cfg, bot, yk=fake_yk)
    dup_row = await db.get_order(dup_order)
    subs_8001 = await db.fetchall("SELECT * FROM subscriptions WHERE user_id = 8001")
    check("оплата по старой ссылке завершает заказ этим же платежом",
          dup_row["status"] == "paid" and dup_row["payment_id"]
          and any("Оплата получена" in t for t, _ in done_call.edits), dup_row["status"])
    check("выдана ровно одна подписка", len(subs_8001) == 1, str(len(subs_8001)))

    # Просроченный счёт: следующий клик выпускает новый (слот освобождён)
    await db.ensure_user(8002, "stale", "Просрок")
    stale_order = await db.create_order(8002, "1m", 150, 150, None, "none", "subscription")
    c1 = FakeCall(stale_order, 8002)
    await user_mod.cb_pay(c1, db, cfg, bot, yk=fake_yk)
    first_pid = (await db.get_order(stale_order))["payment_id"]
    fake_yk.statuses[first_pid] = "canceled"
    c2 = FakeCall(stale_order, 8002)
    await user_mod.cb_pay(c2, db, cfg, bot, yk=fake_yk)
    second_pid = (await db.get_order(stale_order))["payment_id"]
    check("отменённый счёт заменяется новым при повторной попытке",
          second_pid != first_pid and c2.checkout_url.endswith(second_pid),
          f"{first_pid} -> {second_pid}")
    # Чужой пользователь не получает счёт
    c3 = FakeCall(stale_order, 9999)
    await user_mod.cb_pay(c3, db, cfg, bot, yk=fake_yk)
    check("чужому пользователю отказано", "не найден" in " ".join(c3.answers), str(c3.answers))

    print("\n16. Одно окно: переход с фото на текст")
    from app import ui

    class PhotoWindowBot:
        def __init__(self):
            self.events = []

        async def edit_message_text(self, text, chat_id, message_id, **kwargs):
            self.events.append(("edit", message_id))
            raise RuntimeError("there is no text in the message to edit")

        async def delete_message(self, chat_id, message_id):
            self.events.append(("delete", message_id))

        async def send_message(self, chat_id, text, **kwargs):
            self.events.append(("send", text))
            return SimpleNamespace(message_id=902)

    photo_bot = PhotoWindowBot()
    photo_message = SimpleNamespace(chat=SimpleNamespace(id=900), message_id=901)
    ui.forget(900)
    await ui.ui_text_from_message(photo_bot, photo_message, "Текстовое окно")
    check("фото удаляется перед отправкой текстового окна",
          photo_bot.events == [("edit", 901), ("delete", 901), ("send", "Текстовое окно")]
          and ui.get_window(900) == 902,
          str(photo_bot.events))
    ui.forget(900)

    await db.close()
    print(f"\n{'=' * 60}\nИТОГО: {len(PASSED)} успешно, {len(FAILED)} ошибок")
    if FAILED:
        for item in FAILED:
            print(f"  • {item}")
        sys.exit(1)
    print("Все проверки пройдены ✅")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        # aiosqlite держит поток на каждое соединение: закрываемся явно
        import gc

        gc.collect()
