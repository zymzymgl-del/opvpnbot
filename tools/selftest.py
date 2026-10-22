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
from app.config import is_absolute_url, load_config, validate  # noqa: E402
from app.db import Database, from_iso, to_iso, utcnow  # noqa: E402
from app.keyboards import (  # noqa: E402
    BTN_TRIAL,
    admin_service_kb,
    main_menu,
    payment_methods_kb,
    plans_kb,
    profile_kb,
    subscription_kb,
)
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
    _flow_for_inbounds,
    _inbound_supports_flow,
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
    send_subscription_card,
)
from app.utils import human_left  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  ❌ {name} — {detail}")


def _has_trial_button(markup) -> bool:
    """Есть ли в клавиатуре кнопка пробного периода."""
    return any(
        button.text == BTN_TRIAL
        for row in markup.inline_keyboard
        for button in row
    )


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

    print("\n1d2. human_left: корректный разбор остатка (дни/часы/минуты)")
    check("23 ч 59 мин → «23 ч. 59 мин.»",
          human_left(utcnow() + timedelta(hours=23, minutes=59)) == "23 ч. 59 мин.",
          human_left(utcnow() + timedelta(hours=23, minutes=59)))
    check("23 ч ровно → «23 ч. 0 мин.»",
          human_left(utcnow() + timedelta(hours=23)) == "23 ч. 0 мин.",
          human_left(utcnow() + timedelta(hours=23)))
    check("1 д 23 ч 59 мин → «1 д. 23 ч.»",
          human_left(utcnow() + timedelta(days=1, hours=23, minutes=59)) == "1 д. 23 ч.",
          human_left(utcnow() + timedelta(days=1, hours=23, minutes=59)))
    check("59 мин → «59 мин.»",
          human_left(utcnow() + timedelta(minutes=59)) == "59 мин.",
          human_left(utcnow() + timedelta(minutes=59)))
    check("прошедшее время → «истёк»",
          human_left(utcnow() - timedelta(minutes=5)) == "истёк")

    print("\n1e. XUI_SUB_BASE: только полный URL (путь без хоста Telegram отклоняет)")
    check("полный URL подписки принят", is_absolute_url("https://sub.example.com:2096/abc/def"))
    check("путь без хоста отклонён", not is_absolute_url("/hvpa9knwot91fkxb"))
    check("хост без схемы отклонён", not is_absolute_url("sub.example.com:2096/abc"))
    check("пустое значение не считается URL", not is_absolute_url(""))
    check("корректный XUI_SUB_BASE не даёт ошибок конфигурации",
          validate(dataclasses.replace(cfg, xui_sub_base="https://sub.example.com:2096/abc")) == [])
    bad_sub = validate(dataclasses.replace(cfg, xui_sub_base="/hvpa9knwot91fkxb"))
    check("XUI_SUB_BASE без хоста — ошибка конфигурации",
          any("XUI_SUB_BASE" in p for p in bad_sub), "; ".join(bad_sub))
    check("пустой XUI_SUB_BASE допустим (прямые конфиги)",
          validate(dataclasses.replace(cfg, xui_sub_base="")) == [])
    # Кнопка-ссылка только для абсолютного URL: иначе Telegram отклоняет отправку
    # карточки целиком («URL host is empty»), и пользователь не видит ничего.
    abs_kb = subscription_kb(1, "https://sub.example.com:2096/abc", "vless://x")
    abs_urls = [b.url for row in abs_kb.inline_keyboard for b in row if getattr(b, "url", None)]
    check("карточка с полным URL: есть кнопка-ссылка",
          "https://sub.example.com:2096/abc" in abs_urls, str(abs_urls))
    rel_kb = subscription_kb(1, "/hvpa9knwot91fkxb/d14e3ae64c00b83c", "vless://x")
    rel_urls = [b.url for row in rel_kb.inline_keyboard for b in row if getattr(b, "url", None)]
    rel_cbs = {b.callback_data for row in rel_kb.inline_keyboard for b in row}
    check("относительный URL в кнопку не попадает",
          not any(u.startswith("/") for u in rel_urls), str(rel_urls))
    check("карточка при этом собирается (копирование/QR на месте)",
          {"sub:copy:1", "sub:qr:1"} <= rel_cbs, str(rel_cbs))
    # Внешних ссылок на аккаунт поддержки в карточке нет: поддержка ведётся
    # в боте (тикеты на экране «🆘 Поддержка»), а не в личке Telegram.
    check("в карточке нет внешней кнопки поддержки (t.me)",
          not any("t.me" in (u or "") for u in abs_urls), str(abs_urls))

    # Косметика карточки: вместо текста «Нажмите кнопку ниже…» должна стоять
    # кликабельная ссылка на подписку, а при относительном пути — прежняя подсказка.
    card_bot = FakeBot()
    await send_subscription_card(card_bot, cfg, db, 1099, {
        "id": 777, "vpn_key": "vless://x", "sub_url": "https://sub.example.com:2096/abc/def",
        "plan_code": "1m", "expires_at": to_iso(utcnow() + timedelta(days=30)),
        "devices": 2, "traffic_gb": 0,
    })
    card_text = card_bot.last_text_for(1099)
    check("карточка: сама ссылка в моноширинном блоке (тап копирует)",
          "<code>https://sub.example.com:2096/abc/def</code>" in card_text, card_text)
    check("карточка: ссылка не открывается тапом (нет <a href>)",
          "<a href=" not in card_text, card_text)
    check("карточка: старая подсказка про кнопки убрана",
          "Нажмите кнопку ниже" not in card_text, card_text)

    rel_card_bot = FakeBot()
    await send_subscription_card(rel_card_bot, cfg, db, 1098, {
        "id": 778, "vpn_key": "vless://x", "sub_url": "/hvpa9knwot91fkxb/d14e3ae64c00b83c",
        "plan_code": "1m", "expires_at": to_iso(utcnow() + timedelta(days=30)),
        "devices": 2, "traffic_gb": 0,
    })
    rel_card_text = rel_card_bot.last_text_for(1098)
    check("карточка без полного URL: остаётся подсказка про кнопки",
          "Нажмите кнопку ниже" in rel_card_text, rel_card_text)
    check("карточка без полного URL: битой ссылки в тексте нет",
          "/hvpa9knwot91fkxb" not in rel_card_text and "<a href=" not in rel_card_text,
          rel_card_text)

    # Тест ниже хардкодом использует коды "1m"/"3m", а DEFAULT_PLANS в db.py — "1device"/"2device"/…
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
    check("db.trial_used видит активацию", await db.trial_used(1003) is True)
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
    check("flow применим к vless tcp+tls/reality", all(_inbound_supports_flow(ib) for ib in (
        {"protocol": "vless", "streamSettings": '{"network": "tcp", "security": "reality"}'},
        {"protocol": "vless", "streamSettings": '{"network": "tcp", "security": "tls"}'},
    )))
    check("flow не применим к ws/xhttp/grpc/trojan/vmess", not any(_inbound_supports_flow(ib) for ib in (
        {"protocol": "vless", "streamSettings": '{"network": "ws", "security": "tls"}'},
        {"protocol": "vless", "streamSettings": '{"network": "xhttp", "security": "reality"}'},
        {"protocol": "vless", "streamSettings": '{"network": "grpc", "security": "tls"}'},
        {"protocol": "trojan", "streamSettings": '{"network": "tcp", "security": "tls"}'},
        {"protocol": "vmess", "streamSettings": '{"network": "tcp", "security": "tls"}'},
    )))
    check("flow берётся из reality-инбаунда, даже если он не первый", _flow_for_inbounds([
        {"protocol": "vless", "streamSettings": '{"network": "ws", "security": "tls"}', "settings": '{"clients": []}'},
        {"protocol": "vless", "streamSettings": '{"network": "tcp", "security": "reality"}',
         "settings": '{"clients": [{"flow": "xtls-rprx-vision"}]}'},
    ]) == "xtls-rprx-vision")
    check("пустой flow в шаблоне заменяется на vision", _flow_for_inbounds([
        {"protocol": "vless", "streamSettings": '{"network": "tcp", "security": "reality"}',
         "settings": '{"clients": [{"flow": ""}]}'},
    ]) == "xtls-rprx-vision")
    check("без подходящих inbound-ов flow пуст", _flow_for_inbounds([
        {"protocol": "vless", "streamSettings": '{"network": "ws", "security": "tls"}'},
    ]) == "")
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

    print("\n8c. flow для reality-инбаунда (per-inbound в 3.x)")
    ws_with_flow = _build_link(ws_inbound, {"id": "uuid-2", "flow": "xtls-rprx-vision", "email": "u2"}, "example.com")
    check("ссылка ws не получает flow", "flow=" not in ws_with_flow, ws_with_flow)
    reality_with_flow = _build_link(
        vless_inbound, {"id": "uuid-1", "flow": "xtls-rprx-vision", "email": "u1"}, "1.2.3.4")
    check("ссылка reality получает flow", "flow=xtls-rprx-vision" in reality_with_flow, reality_with_flow)

    flow_panel = XuiPanel(load_config())
    api_calls: list[tuple[str, dict | None]] = []

    async def fake_panel_api(path, *, method="GET", payload=None):
        api_calls.append((path, payload))
        return {"success": True}

    async def fake_panel_get_client(email):
        return {"uuid": "uuid-flow", "flow": "xtls-rprx-vision", "subId": "sub-flow", "email": email}

    flow_panel._api = fake_panel_api
    flow_panel._get_client = fake_panel_get_client
    issued = await flow_panel._create_client_clients_api(
        [vless_inbound, ws_inbound], user_id=9001, days=30, devices=1, traffic_gb=10, plan_code="1m")
    add_payload = api_calls[0][1] or {}
    check("в /clients/add уходит flow",
          add_payload.get("client", {}).get("flow") == "xtls-rprx-vision", str(add_payload.get("client")))
    check("в /clients/add перечислены все inbound-ы", add_payload.get("inboundIds") == [1, 2],
          str(add_payload.get("inboundIds")))
    flow_links = issued.vpn_key.splitlines()
    check("reality-ссылка с flow, ws-ссылка без flow",
          len(flow_links) == 2 and "flow=xtls-rprx-vision" in flow_links[0] and "flow=" not in flow_links[1],
          issued.vpn_key)

    print("\n9. Клавиатуры и статистика")
    check("главное меню собирается", main_menu(True, True) is not None)
    check("кнопка триала есть, пока он не использован", _has_trial_button(main_menu(False, True)))
    check("кнопка триала скрыта после активации", not _has_trial_button(main_menu(False, False)))
    check("список тарифов собирается", plans_kb(await db.active_plans()) is not None)
    check("выбор способа оплаты собирается", payment_methods_kb(["yookassa"], 1) is not None)
    stats = await db.stats()
    check("статистика считается", stats["users_total"] >= 3 and stats["revenue_total"] >= 299, str(stats["revenue_total"]))


    print("\n9b. Админ-список пользователей: индикаторы подписок")
    rows = await db.users_page(100, 0)
    by_id = {int(row["telegram_id"]): row for row in rows}
    active_row = by_id.get(1001)
    inactive_row = by_id.get(3001)
    no_sub_row = by_id.get(1003)
    check("клиент с активной подпиской: subs > 0 (🟢)",
          active_row is not None and int(active_row["subs"]) >= 1,
          str(dict(active_row)) if active_row else "нет строки 1001")
    check("отключённый/истёкший клиент: подписки есть, действующих нет (🔴)",
          inactive_row is not None and int(inactive_row["subs"]) == 0
          and int(inactive_row["subs_total"]) >= 1,
          str(dict(inactive_row)) if inactive_row else "нет строки 3001")
    check("клиент без подписок: оба счётчика нулевые",
          no_sub_row is not None and int(no_sub_row["subs"]) == 0
          and int(no_sub_row["subs_total"]) == 0,
          str(dict(no_sub_row)) if no_sub_row else "нет строки 1003")

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
    from app.webhook import YOOKASSA_NETS, _ip_allowed, _is_loopback, _order_by_metadata
    check("IP ЮKassa из белого списка разрешён", _ip_allowed("185.71.76.10"))
    check("посторонний IP отклонён", not _ip_allowed("8.8.8.8"))
    check("диапазоны IP заданы", len(YOOKASSA_NETS) >= 6)
    # Диагностика частой ошибки: бот за nginx, а WEBHOOK_TRUST_PROXY=false.
    check("127.0.0.1 распознаётся как reverse-proxy", _is_loopback("127.0.0.1"))
    check("::1 распознаётся как reverse-proxy", _is_loopback("::1"))
    check("IP ЮKassa не считается reverse-proxy", not _is_loopback("185.71.76.10"))
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

    # 13b-2. Бэкап из админки: консистентный снимок, а не «живой» файл в WAL
    from app.handlers import admin as admin_mod

    class BackupCall:
        def __init__(self) -> None:
            self.answers: list[str] = []
            self.docs: list[tuple[str, bool, str | None]] = []
            self.sent_path: Path | None = None
            outer = self

            class _Msg:
                async def answer(self, text, **kw):
                    outer.answers.append(text)

                async def answer_document(self, document, caption=None, **kw):
                    # Читаем снимок ДО удаления временного файла в finally.
                    path = Path(document.path)
                    outer.sent_path = path
                    con = sqlite3.connect(str(path))
                    try:
                        row = con.execute(
                            "SELECT value FROM settings WHERE key = 'backup_marker'"
                        ).fetchone()
                    finally:
                        con.close()
                    outer.docs.append((path.name, _sqlite_backup_ok(path), row[0] if row else None))
                    return SimpleNamespace(message_id=1)

            self.message = _Msg()

        async def answer(self, text="", **kw):
            self.answers.append(text)

    # Маркер пишем непосредственно перед бэкапом: если бы отправлялся «живой»
    # файл без WAL, свежая запись могла бы в снимок не попасть.
    await db.set_setting("backup_marker", "fresh-snapshot")
    backup_call = BackupCall()
    await admin_mod.cb_backup(backup_call, db, cfg)
    check("админ-бэкап: файл отправлен", len(backup_call.docs) == 1, str(backup_call.docs))
    if backup_call.docs:
        name, valid, marker = backup_call.docs[0]
        check("админ-бэкап: валидная SQLite-копия с таблицами", valid, name)
        check("админ-бэкап: снимок содержит свежую запись", marker == "fresh-snapshot", str(marker))
        check("админ-бэкап: понятное имя файла",
              name.startswith("opvpnbot-backup-") and name.endswith(".sqlite3"), name)
    check("админ-бэкап: временный файл удалён",
          backup_call.sent_path is not None and not backup_call.sent_path.exists(),
          str(backup_call.sent_path))
    await db.execute("DELETE FROM settings WHERE key = 'backup_marker'")

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
    # Подсказки должны называть причину, а не просто «задан неверно»: с таким
    # сообщением бот падал в рестарт-цикле, и причину искали чтением .env.
    hint_placeholder = " ".join(_validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="<openssl rand -hex 32>")))
    check("подсказка распознаёт плейсхолдер из инструкции", "плейсхолдер" in hint_placeholder, hint_placeholder)
    hint_space = " ".join(_validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="abc12345 # мой секрет")))
    check("подсказка распознаёт пробел/комментарий в значении", "пробел" in hint_space, hint_space)
    hint_cyr = " ".join(_validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="секретик-123")))
    check("подсказка распознаёт кириллицу", "латиницы" in hint_cyr, hint_cyr)
    hint_short = " ".join(_validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="abc123")))
    check("подсказка называет минимальную длину", "минимум 8" in hint_short, hint_short)
    hint_empty = " ".join(_validate_tg_webhook(_dc.replace(tg_on, tg_webhook_secret="")))
    check("пустой секрет: сказано, что делать", "сгенерируйте" in hint_empty, hint_empty)
    check("секрет сверяется побайтово", _secret_ok("a" * 32, "a" * 32))
    check("неверный секрет отклоняется", not _secret_ok("b" * 32, "a" * 32))
    check("пустой ожидаемый секрет не пропускает запрос", not _secret_ok("", ""))

    # 13c-2. Таймаут запросов к Telegram API (TG_REQUEST_TIMEOUT): на медленном
    # канале/прокси 60 с не хватает, и апдейты падают с TelegramNetworkError.
    from aiogram.client.session.aiohttp import AiohttpSession as _TgSession
    from aiogram.client.session.base import DEFAULT_TIMEOUT as _TG_DEFAULT

    from app.config import _float as _cfg_float

    check("таймаут Telegram по умолчанию совпадает с дефолтом aiogram",
          _TG_DEFAULT == 60.0 and _cfg_float("TG_REQUEST_TIMEOUT", _TG_DEFAULT) == 60.0)
    os.environ["TG_REQUEST_TIMEOUT"] = "90"
    try:
        check("TG_REQUEST_TIMEOUT читается из окружения",
              _cfg_float("TG_REQUEST_TIMEOUT", _TG_DEFAULT) == 90.0)
    finally:
        os.environ.pop("TG_REQUEST_TIMEOUT", None)
    os.environ["TG_REQUEST_TIMEOUT"] = "мусор"
    try:
        check("мусор в TG_REQUEST_TIMEOUT не роняет конфиг (берётся дефолт)",
              _cfg_float("TG_REQUEST_TIMEOUT", _TG_DEFAULT) == 60.0)
    finally:
        os.environ.pop("TG_REQUEST_TIMEOUT", None)
    _tg_session = _TgSession(timeout=90)
    check("значение доезжает до HTTP-сессии бота", _tg_session.timeout == 90)

    # 13c-3. TelegramNetworkError (сеть до api.telegram.org) — не будим админов.
    import aiogram.methods as _tg_methods
    from aiogram.exceptions import TelegramNetworkError

    import app.middlewares as _mw_mod

    _spam: list[str] = []
    _orig_notify = _mw_mod.notify_admins

    async def _spy_notify(_bot, _cfg, text, *a, **kw):
        _spam.append(text)

    from aiogram.types import CallbackQuery as _CbQuery, Chat as _Chat, Message as _Message, User as _User

    _answers: list[str] = []

    class _StubCall(_CbQuery):
        """Ненастоящий только по транспорту: ответ на нажатие пишем в _answers.

        Наследуемся от CallbackQuery, иначе мидлварь не увидит нажатие
        (isinstance-проверка) и ветка уведомления пользователя не выполнится.
        """

        def __init__(self) -> None:
            super().__init__(
                id="42",
                from_user=_User(id=777, is_bot=False, first_name="Тест"),
                chat_instance="chat-instance",
                data="menu:main",
                message=_Message(message_id=7, date=utcnow(), chat=_Chat(id=777, type="private")),
            )

        async def answer(self, text="", **kw):
            _answers.append(text)

    async def _raise_net(_event, _data):
        raise TelegramNetworkError(method=_tg_methods.GetMe(), message="Request timeout error")

    async def _raise_boom(_event, _data):
        raise RuntimeError("boom")

    _mw = _mw_mod.ErrorsMiddleware(cfg)
    _mw_mod.notify_admins = _spy_notify
    try:
        _net_res = await _mw(_raise_net, _StubCall(), {"bot": bot})
        check("сетевой сбой Telegram не спамит админов", _net_res is None and not _spam,
              "; ".join(_spam))
        check("пользователю предложено повторить",
              any("не отвечает" in a for a in _answers), str(_answers))
        await _mw(_raise_boom, _StubCall(), {"bot": bot})
        check("обычная ошибка по-прежнему уходит админам",
              any("Ошибка в боте" in s for s in _spam), str(_spam))
    finally:
        _mw_mod.notify_admins = _orig_notify

    # 13d. SQL-инъекции: внешние значения (включая поля вебхука) идут только
    # параметрами; имена столбцов — из белых списков. Проверяем «боевыми» строками.
    injected = "x' OR '1'='1' --"
    order_id_inj = await db.create_order(
        user_id=9101, plan_code="1m", amount=1, base_amount=1, promo_code=None,
        method="manual", payment_id=injected,
    )
    check("SQL-мусор в payment_id сохраняется как обычный текст",
          (await db.get_order(order_id_inj))["payment_id"] == injected)
    found_order = await db.order_by_payment(injected)
    check("поиск заказа по подставленной строке находит только свой заказ",
          found_order is not None and int(found_order["id"]) == order_id_inj)
    check("похожий платёж не зачитывается как чужой заказ",
          await db.order_by_payment("x' OR '1'='1' --zzz") is None)
    check("подстановка в order_id не проходит через int()",
          await db.get_order(int("0")) is None)

    await db.ensure_user(9102, injected, "Тест")
    check("подставленное значение не подменяет условие поиска пользователя",
          await db.find_user_by_username("x' OR '1'='1") is None
          and (await db.find_user_by_username(injected)) is not None)

    field_rejected = False
    try:
        await db.set_user_field(9102, "is_banned = 1 --", 1)
    except ValueError:
        field_rejected = True
    check("имя столбца вне белого списка отклоняется",
          field_rejected and int((await db.get_user(9102))["is_banned"]) == 0)

    users_before = await db.count_users()
    await db.update_subscription(999999, **{"status = 'x' WHERE 1=1 --": "y"})
    check("незнакомый ключ update_subscription отброшен (таблица цела)",
          await db.count_users() == users_before)

    # 13e. Расширенный /healthz: heartbeat, кэш панели, deep и совместимость
    import app.health as health_mod

    check("health: deep по умолчанию выключен (прежнее поведение)",
          cfg.health_deep is False)
    check("health: панель проверяется, кэш 60 с, таймаут 5 с",
          cfg.health_panel_check is True and cfg.health_panel_ttl == 60
          and cfg.health_panel_timeout == 5.0)
    check("health: порог воркера 600 с, апдейтов — выключен (0)",
          cfg.health_max_worker_age == 600 and cfg.health_max_update_age == 0)

    health_mod.reset_state()
    health_mod.touch_update()
    health_mod.touch_worker()
    hb = health_mod.heartbeat()
    check("health: heartbeat обновляется", hb["update_age"] < 1 and hb["worker_age"] < 1, str(hb))

    # Совместимость: без deep панель/воркер/апдейты не влияют на статус.
    ok, reasons = health_mod.evaluate(
        cfg, db_ok=True, panel={"ok": False, "message": "down", "checked_ago": 0.0},
        hb={"update_age": 10**6, "worker_age": 10**6},
    )
    check("health без deep: статус зависит только от БД", ok is True and reasons == [],
          f"{ok} {reasons}")

    deep = dataclasses.replace(cfg, health_deep=True,
                               health_max_worker_age=600, health_max_update_age=1800)
    ok, reasons = health_mod.evaluate(
        deep, db_ok=True, panel={"ok": False, "message": "down", "checked_ago": 0.0},
        hb={"update_age": 0.0, "worker_age": 0.0},
    )
    check("health deep: панель недоступна → не healthy", ok is False and "panel" in reasons,
          str(reasons))
    ok, reasons = health_mod.evaluate(
        deep, db_ok=True, panel={"ok": True, "message": "", "checked_ago": 0.0},
        hb={"update_age": 0.0, "worker_age": 601.0},
    )
    check("health deep: застрявший воркер → не healthy",
          ok is False and "worker" in reasons, str(reasons))
    ok, reasons = health_mod.evaluate(
        deep, db_ok=True, panel={"ok": True, "message": "", "checked_ago": 0.0},
        hb={"update_age": 1801.0, "worker_age": 0.0},
    )
    check("health deep: нет апдейтов дольше порога → не healthy",
          ok is False and "updates" in reasons, str(reasons))
    ok, reasons = health_mod.evaluate(
        deep, db_ok=False, panel={"ok": True, "message": "", "checked_ago": 0.0},
        hb={"update_age": 0.0, "worker_age": 0.0},
    )
    check("health deep: БД недоступна — причина db", ok is False and reasons == ["db"], str(reasons))

    class _HealthPanel:
        def __init__(self) -> None:
            self.calls = 0
            self.ok = True
            self.delay = 0.0

        async def health(self):
            self.calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.ok, ("ok" if self.ok else "панель недоступна")

    _hp = _HealthPanel()
    _orig_get_panel = health_mod.get_panel
    health_mod.get_panel = lambda _cfg, _db: _hp
    try:
        health_mod.reset_state()
        cached_cfg = dataclasses.replace(cfg, health_panel_ttl=60, health_panel_timeout=1.0)
        r1 = await health_mod._check_panel_cached(cached_cfg, db)
        r2 = await health_mod._check_panel_cached(cached_cfg, db)
        check("health: результат панели кэшируется",
              _hp.calls == 1 and r1["ok"] is True and r2["ok"] is True, f"calls={_hp.calls}")
        check("health: возраст кэша отдаётся",
              r2["checked_ago"] is not None and r2["checked_ago"] >= 0, str(r2))

        health_mod.reset_state()
        _hp.ok = False
        r3 = await health_mod._check_panel_cached(cached_cfg, db)
        check("health: недоступная панель отдаётся как ошибка", r3["ok"] is False, str(r3))

        health_mod.reset_state()
        _hp.ok = True
        _hp.delay = 0.2
        slow_cfg = dataclasses.replace(cfg, health_panel_ttl=0, health_panel_timeout=0.01)
        r4 = await health_mod._check_panel_cached(slow_cfg, db)
        check("health: таймаут панели не вешает health",
              r4["ok"] is False and "не ответила" in r4["message"], str(r4))
    finally:
        _hp.delay = 0.0
        health_mod.get_panel = _orig_get_panel
        health_mod.reset_state()

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
    # Косметика выдачи: карточка подписки и сухой текст пишутся в ОДНО окно,
    # поэтому текст «ключ отправлен отдельным сообщением» затирал карточку, и
    # пользователь оставался без ссылки, QR и кнопок. Проверяем, что в окне
    # осталась именно карточка с кнопками конфига.
    card_markup = done_call.edits[-1][1] if done_call.edits else None
    card_cbs = {b.callback_data for row in (card_markup.inline_keyboard if card_markup else [])
                for b in row if getattr(b, "callback_data", None)}
    check("после оплаты в окне карточка подписки (кнопки конфига)",
          any(str(cb).startswith("sub:copy:") for cb in card_cbs), str(card_cbs))
    check("сухой текст «отдельным сообщением» не показывается",
          not any("отдельным сообщением" in t for t, _ in done_call.edits),
          str([t[:60] for t, _ in done_call.edits]))

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
