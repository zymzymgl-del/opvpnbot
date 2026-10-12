#!/usr/bin/env python3
"""Интеграционный тест: прогоняет апдейты через настоящий диспетчер aiogram.

Сеть и Telegram не нужны: Bot и Message подменяются заглушками, а апдейты
подаются через ``Dispatcher.feed_update``. Проверяются: маршрутизация апдейтов,
мидлвари, выдача ключей, промокоды, админ-панель.

Запуск:  python tools/integration_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="opvpnbot-it-")
os.environ.update({
    "BOT_TOKEN": "123456:TEST-TOKEN",
    "ADMIN_IDS": "777",
    "DB_PATH": str(Path(TMP) / "it.sqlite3"),
    "VPN_PANEL": "xui",
    "XUI_URL": "http://127.0.0.1:2053",
    "XUI_API_TOKEN": "test-token",
    "PAYMENT_METHODS": "yookassa",
    "TRIAL_ENABLED": "true",
    "TRIAL_DAYS": "3",
    "CLAIM_ENABLED": "true",
    "CLAIM_SECRET": "it-secret",
    "MAX_PENDING_ORDERS": "5",
    "THROTTLING_RATE": "0",  # в тесте не тормозим
    "LOG_LEVEL": "ERROR",
    "LOG_FILE": str(Path(TMP) / "bot.log"),
})

from aiogram import Bot  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402

from app.config import load_config  # noqa: E402
from app.db import Database, to_iso, utcnow  # noqa: E402
from app.runtime import runtime as rt  # noqa: E402
from bot import build_dispatcher  # noqa: E402

OUT: list[tuple[str, str]] = []          # (вид, текст)
CALLBACK_ANSWERS: list[str] = []
EDITS: list[str] = []                     # тексты, показанные правкой сообщения (режим одного окна)


def install_stubs(bot: Bot) -> None:
    """Перехватывает вызовы Telegram API на уровне класса Bot.

    В aiogram 3 шорткаты (`message.answer`, `call.answer`, `bot.send_message`)
    возвращают объект метода, который при await вызывает ``Bot.__call__`` —
    поэтому подменяем именно его.
    """

    async def fake_call(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            text = getattr(method, "text", "") or ""
            if text != "\u2063":  # служебное сообщение для снятия reply-клавиатуры
                OUT.append(("message", text))
            sent = Message.model_construct(
                message_id=abs(hash(text)) % 100000 + 1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=getattr(method, "chat_id", 0), type="private"),
                from_user=User(id=1, is_bot=True, first_name="TestBot"),
                text=text, html_text="", entities=[],
            )
            return sent.as_(self)
        elif name == "SendPhoto":
            OUT.append(("photo", getattr(method, "caption", "") or ""))
        elif name == "SendDocument":
            OUT.append(("document", getattr(method, "caption", "") or ""))
        elif name == "SendInvoice":
            OUT.append(("invoice", f"{getattr(method, 'title', '')} / {getattr(method, 'prices', '')}"))
        elif name == "EditMessageText":
            text = getattr(method, "text", "") or ""
            EDITS.append(text)
            OUT.append(("message", text))
        elif name in ("AnswerCallbackQuery", "AnswerPreCheckoutQuery"):
            text = getattr(method, "text", None)
            if text:
                CALLBACK_ANSWERS.append(text)
            return True
        elif name in ("SetMyCommands", "DeleteMyCommands"):
            return True
        return None

    async def get_me(self, *a, **kw):
        return User(id=1, is_bot=True, first_name="TestBot", username="opvpnbot_test_bot")

    Bot.__call__ = fake_call            # type: ignore[method-assign]
    Bot.get_me = get_me                 # type: ignore[method-assign]


def make_message(user_id: int, text: str, message_id: int = 1) -> Update:
    user = User(id=user_id, is_bot=False, first_name="Тест", username=f"user{user_id}")
    chat = Chat(id=user_id, type="private")
    message = Message.model_construct(
        message_id=message_id, date=datetime.now(timezone.utc), chat=chat, from_user=user,
        text=text, html_text=text, entities=[], caption=None,
    )
    return Update.model_construct(update_id=message_id, message=message)


def make_callback(user_id: int, data: str, message_id: int = 1, text: str = "") -> Update:
    user = User(id=user_id, is_bot=False, first_name="Тест", username=f"user{user_id}")
    chat = Chat(id=user_id, type="private")
    message = Message.model_construct(
        message_id=message_id, date=datetime.now(timezone.utc), chat=chat, from_user=user,
        text=text or "…", html_text=text or "…", entities=[], caption=None,
    )
    call = CallbackQuery.model_construct(
        id=str(message_id), from_user=user, chat_instance="ci", message=message, data=data, inline_message_id=None,
    )
    return Update.model_construct(update_id=10000 + message_id, callback_query=call)


def sent(kind: str) -> str:
    return "\n".join(text for k, text in OUT if k == kind)


async def run() -> None:
    failures: list[str] = []

    def expect(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'✅' if condition else '❌'} {name}")
        if not condition:
            failures.append(f"{name}: {detail}")

    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()
    await rt.load(db, cfg)
    # Тест использует коды "1m"/"3m", а DEFAULT_PLANS в db.py — "2devic"/"3devic"/…
    await db.upsert_plan("1m", "1 месяц", 150, 30, 0, 2)
    await db.upsert_plan("3m", "3 месяца", 405, 90, 0, 3)

    bot = Bot(cfg.bot_token)
    install_stubs(bot)

    import app.services.panels as panels_mod
    from app.services.panels import BasePanel, IssuedKey

    class FakePanel(BasePanel):
        """Заглушка панели 3x-ui: выдаёт синтетические ключи без сети."""

        name = "xui"
        supports_claim = False

        def __init__(self) -> None:
            self.counter = 0

        async def create_client(self, *, user_id, days, devices, traffic_gb, plan_code):
            self.counter += 1
            return IssuedKey(vpn_key=f"vless://fake-{user_id}-{self.counter}@srv:443#k",
                             sub_url=None, panel=self.name, panel_username=f"tg{user_id}")

        async def extend_client(self, *, panel_username, days, devices=None, traffic_gb=None):
            return True

        async def set_enabled(self, *, panel_username, enabled):
            return True

        async def delete_client(self, *, panel_username):
            return True

        async def health(self):
            return True, "заглушка"

    panels_mod._panel = FakePanel()

    class FakeYooKassa:
        """Заглушка ЮKassa: платёж создаётся, проверка всегда успешна."""

        async def create_payment(self, *, amount_rub, description, metadata):
            return {"id": "pay-test", "confirmation": {"confirmation_url": "https://yookassa.ru/checkout/test"}}

        async def is_paid(self, payment_id, expected_amount=None):
            return True

    dp = build_dispatcher(cfg, db, bot, FakeYooKassa())
    dp["bot_username"] = "opvpnbot_test_bot"

    print("\n1. Пользовательский поток")
    OUT.clear()
    await dp.feed_update(bot, make_message(555, "/start"))
    expect("на /start пришло приветствие", "Добро пожаловать" in sent("message"), sent("message")[:80])

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "menu:buy", 2))
    expect("магазин показан", "Выберите тариф" in sent("message"))

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:1m", 3))
    expect("заказ создан", "Заказ #" in sent("message"), sent("message")[:80])
    order_id = await db.scalar("SELECT id FROM orders WHERE user_id = 555 ORDER BY id DESC LIMIT 1")
    expect("в БД есть заказ kind=subscription",
           (await db.get_order(int(order_id)))["kind"] == "subscription")

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, f"pay:yookassa:{order_id}", 4))
    expect("оплата картой/СБП предложена", "Оплата заказа" in sent("message"), sent("message")[:80])

    # баланс вырезан: в профиле нет кнопки пополнения, оплата с баланса недоступна
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "menu:profile", 16))
    expect("профиль показан", "Профиль" in sent("message"))
    expect("в профиле нет кнопки пополнения баланса", "Пополнить баланс" not in sent("message"))

    OUT.clear()
    CALLBACK_ANSWERS.clear()
    await dp.feed_update(bot, make_callback(555, f"pay:balance:{order_id}", 6))
    expect("оплата с баланса недоступна", "Ключ отправлен" not in sent("message"), sent("message")[:120])

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:1m", 5))
    orders_count = int(await db.scalar("SELECT COUNT(*) FROM orders WHERE user_id = 555"))
    expect("повторный buy переиспользовал счёт (не плодит заказы)", orders_count == 1, str(orders_count))

    # заказ подтверждает админ — появляется подписка (нужна для дальнейших шагов)
    OUT.clear()
    await dp.feed_update(bot, make_callback(777, f"adm:order:approve:{order_id}", 6))
    sub = await db.active_subscription(555)
    expect("подписка выдана панелью", sub is not None and "fake" in (sub["vpn_key"] or ""))

    # продление доступно только в окне за 3 дня до окончания
    OUT.clear()
    CALLBACK_ANSWERS.clear()
    await dp.feed_update(bot, make_callback(555, "buy:1m", 17))
    expect("продление вне окна заблокировано",
           "Заказ #" not in sent("message") and any("окончани" in a for a in CALLBACK_ANSWERS),
           sent("message")[:80])
    await db.update_subscription(sub["id"], expires_at=to_iso(utcnow() + timedelta(days=2)))

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "menu:keys", 18))
    expect("список ключей показан", "Мои подписки" in sent("message"), sent("message")[:80])
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, f"sub:view:{sub['id']}", 7))
    expect("карточка подписки показана", "Подписка #" in sent("message"))

    print("\n2. Промокод через бота")
    await db.create_promo("SALE25", "percent", 25, max_uses=5, expires_at=None)
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:3m", 8))
    promo_order = int(await db.scalar("SELECT id FROM orders WHERE user_id = 555 ORDER BY id DESC LIMIT 1"))
    EDITS.clear()
    await dp.feed_update(bot, make_callback(555, f"promo:order:{promo_order}", 9))
    expect("подсказка промокода — правка окна (одно окно)",
           any("Введите промокод" in t for t in EDITS), str(EDITS)[:120])
    EDITS.clear()
    await dp.feed_update(bot, make_message(555, "sale25", 10))
    order = await db.get_order(promo_order)
    expect("скидка 25% применена", int(order["amount"]) == 405 - 101, str(order["amount"]))
    expect("сообщение об успехе промокода", "применён" in sent("message"), sent("message")[:120])
    expect("результат промокода — правка окна (одно окно)",
           any("применён" in t for t in EDITS), str(EDITS)[:120])
    promo_used = int(await db.scalar("SELECT used FROM promos WHERE code = 'SALE25'"))
    expect("применение промокода резервирует его", promo_used == 1, str(promo_used))

    # отмена заказа освобождает резерв промокода
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, f"order:cancel:{promo_order}", 40))
    expect("заказ отменён", "Заказ отменён" in sent("message"), sent("message")[:120])
    promo_after_cancel = int(await db.scalar("SELECT used FROM promos WHERE code = 'SALE25'"))
    expect("отмена освобождает промокод", promo_after_cancel == 0, str(promo_after_cancel))

    # оформляем заказ заново с промокодом — списание при оплате проверит раздел 3
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:3m", 41))
    promo_order = int(await db.scalar("SELECT id FROM orders WHERE user_id = 555 ORDER BY id DESC LIMIT 1"))
    await dp.feed_update(bot, make_callback(555, f"promo:order:{promo_order}", 42))
    await dp.feed_update(bot, make_message(555, "sale25", 43))
    promo_reserved = int(await db.scalar("SELECT used FROM promos WHERE code = 'SALE25'"))
    expect("повторное применение после отмены снова резервирует промокод",
           promo_reserved == 1, str(promo_reserved))

    print("\n2.5 Оплата через ЮKassa")
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:1m", 12))
    yk_order = int(await db.scalar("SELECT id FROM orders WHERE user_id = 555 ORDER BY id DESC LIMIT 1"))
    await dp.feed_update(bot, make_callback(555, f"pay:yookassa:{yk_order}", 13))
    expect("экран оплаты ЮKassa показан", "Оплата заказа" in sent("message"), sent("message")[:140])
    await dp.feed_update(bot, make_callback(555, f"pay:check:{yk_order}", 14))
    expect("оплата ЮKassa подтверждена и ключ выдан", "Оплата получена" in sent("message"), sent("message")[:140])

    print("\n3. Админ-панель")
    OUT.clear()
    await dp.feed_update(bot, make_message(777, "/admin"))
    expect("админ-панель открыта", "Админ-панель" in sent("message"), sent("message")[:120])

    OUT.clear()
    await dp.feed_update(bot, make_callback(777, "adm:stats", 11))
    expect("статистика показана", "Статистика" in sent("message"))

    OUT.clear()
    await dp.feed_update(bot, make_callback(777, "adm:paneltest", 12))
    expect("проверка панели показана", "Проверка панели" in sent("message"))

    OUT.clear()
    await dp.feed_update(bot, make_callback(777, "adm:orders", 13))
    expect("список заказов показан", "Заказы" in sent("message"), sent("message")[:80])

    # подтверждение заказа админом
    approve_order = int(await db.scalar(
        "SELECT id FROM orders WHERE status = 'pending' AND kind = 'subscription' ORDER BY id LIMIT 1"))
    OUT.clear()
    await dp.feed_update(bot, make_callback(777, f"adm:order:approve:{approve_order}", 14))
    approved = await db.get_order(approve_order)
    expect("заказ подтверждён админом", approved["status"] == "paid", approved["status"])
    promo_used_paid = int(await db.scalar("SELECT used FROM promos WHERE code = 'SALE25'"))
    expect("промокод списан при оплате", promo_used_paid == 1, str(promo_used_paid))

    # обычный пользователь не имеет доступа к админ-колбэкам
    OUT.clear()
    CALLBACK_ANSWERS.clear()
    await dp.feed_update(bot, make_callback(555, "adm:stats", 15))
    expect("админ-колбэк недоступен обычному пользователю",
           "Статистика" not in sent("message"),
           sent("message")[:80])

    print("\n4. Прочее")
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "menu:support", 24))
    expect("раздел поддержки открыт", "Поддержка" in sent("message"))
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "support:faq", 16))
    expect("FAQ показан", "Частые вопросы" in sent("message"))
    OUT.clear()
    await dp.feed_update(bot, make_message(555, "🛒 Купить VPN", 27))
    expect("старая reply-кнопка открывает инлайн-меню",
           "Главное меню" in sent("message"), sent("message")[:80])
    print("\n4a. Реферальная программа вырезана")
    OUT.clear()
    CALLBACK_ANSWERS.clear()
    await dp.feed_update(bot, make_callback(555, "ref:open", 25))
    expect("реферальный раздел не открывается",
           "Приглашайте друзей" not in sent("message"), sent("message")[:120])
    OUT.clear()
    await dp.feed_update(bot, make_message(999, "/start ref_555", 18))
    expect("реферальная метка в /start игнорируется",
           "Приглашайте" not in sent("message"), sent("message")[:120])

    print("\n4b. Анти-абьюз: лимит неоплаченных заказов")
    abuse_user = 6100
    await db.ensure_user(abuse_user, "abuser", "Абьюзер")
    for _ in range(5):
        await db.create_order(abuse_user, "1m", 150, 150, None, "none")
    CALLBACK_ANSWERS.clear()
    OUT.clear()
    await dp.feed_update(bot, make_callback(abuse_user, "buy:3m", 30))
    expect("превышение лимита неоплаченных заказов заблокировано",
           any("неоплаченных" in a for a in CALLBACK_ANSWERS), str(CALLBACK_ANSWERS))
    abuse_orders = int(await db.scalar(
        "SELECT COUNT(*) FROM orders WHERE user_id = ?", (abuse_user,)))
    expect("новый заказ при превышении лимита не создан", abuse_orders == 5, str(abuse_orders))

    print("\n5. Привязка существующего клиента панели (QR/claim)")
    import app.services.panels as panels_mod
    from app.services.panels import BasePanel, ClaimedClient
    from app.services.claims import claim_token

    class _StubPanel(BasePanel):
        name = "xui"
        supports_claim = True

        async def create_client(self, **kw):  # pragma: no cover - не используется
            raise NotImplementedError

        async def list_claimable(self):
            return [ClaimedClient(
                email="legacy1", sub_id="LEGACY-1", vpn_key="vless://legacy@h:443#legacy",
                sub_url=None, expires_at=utcnow() + timedelta(days=20),
                devices=1, traffic_gb=0, enabled=True,
            )]

    saved_panel = panels_mod._panel
    panels_mod._panel = _StubPanel()
    try:
        OUT.clear()
        await dp.feed_update(bot, make_callback(777, "adm:claim", 20))
        expect("админ: запрошен subId/email клиента", "QR для привязки" in sent("message"), sent("message")[:120])

        OUT.clear()
        await dp.feed_update(bot, make_message(777, "LEGACY-1", 21))
        expect("админ: ссылка для клиента сформирована", "Ссылка для привязки" in sent("message"), sent("message")[:120])
        expect("админ: ссылка ведёт на бота", "start=claim_" in sent("message"), sent("message")[:200])

        token = claim_token("LEGACY-1", "it-secret")
        OUT.clear()
        await dp.feed_update(bot, make_message(888, f"/start claim_{token}"))
        sub888 = await db.active_subscription(888)
        expect("привязка: подписка создана", sub888 is not None and sub888["panel_username"] == "legacy1")
        expect("привязка: карточка отправлена", "VPN готов" in sent("message"), sent("message")[:120])

        OUT.clear()
        await dp.feed_update(bot, make_message(889, f"/start claim_{token}"))
        expect("привязка: чужой claim отклонён",
               (await db.active_subscription(889)) is None and "другому аккаунту" in sent("message"),
               sent("message")[:200])
    finally:
        panels_mod._panel = saved_panel

    await db.close()
    await bot.session.close()

    print(f"\n{'=' * 60}")
    if failures:
        print(f"ОШИБКИ ({len(failures)}):")
        for item in failures:
            print(f"  • {item}")
        sys.exit(1)
    print("Интеграционный тест пройден ✅")


if __name__ == "__main__":
    asyncio.run(run())
