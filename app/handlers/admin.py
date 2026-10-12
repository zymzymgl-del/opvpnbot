"""Админ-панель: заказы, пользователи, промокоды, рассылка, тикеты, настройки."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import __author__, __author_url__, __repo__, __version__
from app.config import Config
from app.db import Database, to_iso, utcnow
from app.keyboards import (
    admin_kb,
    admin_order_kb,
    admin_ticket_kb,
    admin_user_kb,
    back_kb,
    broadcast_audience_kb,
    cancel_kb,
    main_menu,
    plan_edit_kb,
    plans_admin_kb,
    settings_kb,
)
from app.runtime import runtime as rt
from app.services.claims import build_claim_link, find_claimable
from app.services.panels import PanelError, get_panel
from app.services.subs import complete_order, grant_subscription, safe_send
from app.utils import STATUS_RU, chunks, esc, fmt_dt, human_left, make_qr_png, price

log = logging.getLogger(__name__)
router = Router(name="admin")

PAGE_SIZE = 8


class IsAdmin(BaseFilter):
    async def __call__(self, event, cfg: Config) -> bool:  # type: ignore[override]
        user = getattr(event, "from_user", None)
        return bool(user and cfg.is_admin(user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminStates(StatesGroup):
    claim_lookup = State()
    promo_code = State()
    promo_type = State()
    promo_value = State()
    promo_uses = State()
    promo_days = State()
    broadcast_text = State()
    user_find = State()
    user_message = State()
    user_grant = State()
    ticket_reply = State()
    plan_value = State()
    trial_days = State()


# ---------------------------------------------------------------------------
# Главное меню админа
# ---------------------------------------------------------------------------
async def _panel_text(db: Database, cfg: Config) -> str:
    stats = await db.stats()
    return (
        "⚙️ <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{stats['users_total']}</b> (+{stats['users_day']} за сутки)\n"
        f"🟢 Активных подписок: <b>{stats['subs_active']}</b>\n"
        f"⏳ Заказов в ожидании: <b>{stats['orders_pending']}</b>\n"
        f"💰 Выручка за сутки: <b>{price(stats['revenue_day'], cfg.currency)}</b> "
        f"| всего: <b>{price(stats['revenue_total'], cfg.currency)}</b>\n"
        f"🧾 Открытых тикетов: <b>{stats['tickets_open']}</b>\n\n"
        f"Панель выдачи: <code>{cfg.panel}</code> · оплата: <code>{', '.join(cfg.payment_methods)}</code>\n\n"
        "💡 <i>Заказы — подтвердить оплаты · Тарифы и триал — в Настройках · "
        "Бэкап — копия базы файлом.</i>"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database, cfg: Config) -> None:
    stats = await db.stats()
    await message.answer(await _panel_text(db, cfg), reply_markup=admin_kb(stats, stats["tickets_open"]))


@router.callback_query(F.data == "adm:panel")
async def cb_admin_panel(call: CallbackQuery, db: Database, cfg: Config) -> None:
    stats = await db.stats()
    try:
        await call.message.edit_text(await _panel_text(db, cfg), reply_markup=admin_kb(stats, stats["tickets_open"]))
    except Exception:
        await call.message.answer(await _panel_text(db, cfg), reply_markup=admin_kb(stats, stats["tickets_open"]))
    await call.answer()


@router.callback_query(F.data == "adm:about")
async def cb_about(call: CallbackQuery, cfg: Config) -> None:
    await call.message.edit_text(
        f"ℹ️ <b>{esc(cfg.bot_name)}</b> v{__version__}\n\n"
        f"🧩 Основа: DF VPN Bot, автор <a href=\"{__author_url__}\">@{__author__}</a>\n"
        "Стек: aiogram 3, SQLite, aiohttp\n"
        f"Панель выдачи: <code>{cfg.panel}</code>\n"
        f"Способы оплаты: <code>{', '.join(cfg.payment_methods)}</code>\n"
        f"Вебхуки ЮKassa: <code>{'включены' if cfg.webhook_enabled else 'выключены'}</code>\n\n"
        f"Исходники: <code>{__repo__}</code>",
        reply_markup=back_kb("adm:panel", "◀️ Назад"),
    )
    await call.answer()


@router.callback_query(F.data == "adm:stats")
async def cb_stats(call: CallbackQuery, db: Database, cfg: Config) -> None:
    stats = await db.stats()
    lines = [
        "📊 <b>Статистика</b>\n",
        f"👥 Пользователей: <b>{stats['users_total']}</b> (за сутки +{stats['users_day']}, "
        f"за неделю +{stats['users_week']})",
        f"🚫 Заблокировано: {stats['banned']}",
        f"🟢 Активных подписок: <b>{stats['subs_active']}</b> | истекших: {stats['subs_expired']}",
        f"🧾 Заказов оплачено: <b>{stats['orders_paid']}</b> | в ожидании: {stats['orders_pending']}",
        f"💰 Выручка: за сутки <b>{price(stats['revenue_day'], cfg.currency)}</b>, "
        f"за 30 дней <b>{price(stats['revenue_month'], cfg.currency)}</b>, "
        f"всего <b>{price(stats['revenue_total'], cfg.currency)}</b>",
    ]
    await call.message.edit_text("\n".join(lines), reply_markup=back_kb("adm:panel", "◀️ Назад"))
    await call.answer()


@router.callback_query(F.data == "adm:paneltest")
async def cb_panel_test(call: CallbackQuery, db: Database, cfg: Config) -> None:
    panel = get_panel(cfg, db)
    ok, message = await panel.health()
    await call.message.answer(
        f"{'✅' if ok else '❌'} <b>Проверка панели</b> (<code>{cfg.panel}</code>)\n\n{esc(message)}",
        reply_markup=back_kb("adm:panel", "◀️ Назад"),
    )
    await call.answer()


@router.callback_query(F.data == "adm:backup")
async def cb_backup(call: CallbackQuery, db: Database, cfg: Config) -> None:
    try:
        document = FSInputFile(cfg.db_path, filename=f"opvpnbot-backup-{utcnow():%Y%m%d-%H%M}.sqlite3")
        await call.message.answer_document(document, caption="💾 Резервная копия базы данных")
    except Exception as exc:
        await call.answer(f"Не удалось отправить файл: {exc}", show_alert=True)
        return
    await call.answer("Готово")


# ---------------------------------------------------------------------------
# Заказы
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:orders")
async def cb_orders(call: CallbackQuery, db: Database, cfg: Config) -> None:
    orders = await db.pending_orders(limit=10)
    if not orders:
        await call.message.edit_text("💰 <b>Заказы</b>\n\nНет заказов, ожидающих оплаты.",
                                     reply_markup=back_kb("adm:panel", "◀️ Назад"))
        await call.answer()
        return
    await call.message.edit_text(
        f"💰 <b>Заказы в ожидании</b> ({len(orders)})\n\nВыберите заказ, чтобы подтвердить или отклонить:",
        reply_markup=back_kb("adm:panel", "◀️ Назад"),
    )
    for order in orders:
        user = await db.get_user(int(order["user_id"]))
        who = user["full_name"] if user else ""
        text = (
            f"🧾 <b>Заказ #{order['id']}</b>\n"
            f"Пользователь: <code>{order['user_id']}</code> {esc(who)}\n"
            f"Позиция: {esc(order['plan_code'])} ({esc(order['kind'])})\n"
            f"Сумма: <b>{price(int(order['amount']), cfg.currency)}</b>\n"
            f"Способ: {esc(order['method'])}\n"
            f"Создан: {fmt_dt(order['created_at'])}"
        )
        await call.message.answer(text, reply_markup=admin_order_kb(int(order["id"])))
    await call.answer()


@router.callback_query(F.data.startswith("adm:order:"))
async def cb_order_action(call: CallbackQuery, db: Database, cfg: Config, bot: Bot) -> None:
    _, _, action, raw_id = call.data.split(":", 3)
    order_id = int(raw_id)
    order = await db.get_order(order_id)
    if order is None:
        await call.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] != "pending":
        await call.answer(f"Заказ уже в статусе: {order['status']}", show_alert=True)
        return

    if action == "approve":
        try:
            await complete_order(bot, cfg, db, order_id, payment_id=f"admin:{call.from_user.id}")
        except PanelError as exc:
            await call.answer(f"Не удалось выдать ключ: {exc}", show_alert=True)
            return
        await call.message.edit_text(f"✅ Заказ #{order_id} подтверждён, ключ выдан клиенту.")
    else:
        if order["promo_code"] and int(order["promo_reserved"] or 0):
            await db.release_promo(order["promo_code"], int(order["user_id"]))
            await db.clear_order_promo_reservation(order_id)
        await db.set_order_status(order_id, "canceled")
        await safe_send(bot, int(order["user_id"]),
                        f"❌ Заказ #{order_id} отклонён администратором. Напишите в поддержку, если это ошибка.")
        await call.message.edit_text(f"❌ Заказ #{order_id} отклонён.")
    await call.answer()


# ---------------------------------------------------------------------------
# Привязка существующего клиента панели (QR для клиента)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:claim")
async def cb_claim(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config) -> None:
    panel = get_panel(cfg, db)
    if not cfg.claim_enabled or not getattr(panel, "supports_claim", False):
        await call.answer("Привязка доступна только при VPN_PANEL=xui и CLAIM_ENABLED=true.", show_alert=True)
        return
    await state.set_state(AdminStates.claim_lookup)
    await call.message.answer(
        "🔗 <b>QR для привязки клиента</b>\n\n"
        "Отправьте <b>subId</b> или <b>email</b> клиента из панели 3x-ui — верну ссылку и QR. "
        "Клиент открывает их в боте, и подписка привязывается к его Telegram.",
        reply_markup=cancel_kb("adm:panel"),
    )
    await call.answer()


@router.message(AdminStates.claim_lookup)
async def msg_claim_lookup(message: Message, state: FSMContext, db: Database, cfg: Config,
                           bot_username: str = "") -> None:
    identifier = (message.text or "").strip()
    await state.clear()
    if not identifier:
        await message.answer("Пустое значение — отправьте subId или email.")
        return
    client = await find_claimable(cfg, db, sub_id=identifier, email=identifier)
    if client is None:
        await message.answer(
            "❌ Клиент не найден в панели. Проверьте subId/email (клиент должен быть в инбаундах панели).",
            reply_markup=back_kb("adm:panel", "◀️ Назад"),
        )
        return
    existing = await db.subscription_by_panel_username("xui", client.email)
    if existing is not None:
        await message.answer(
            f"ℹ️ Клиент <code>{esc(client.email)}</code> уже привязан к <code>{existing['user_id']}</code>.",
            reply_markup=back_kb("adm:panel", "◀️ Назад"),
        )
        return
    if client.expires_at is None:
        await message.answer(
            "❌ У клиента не задан срок в панели — задайте срок и повторите.",
            reply_markup=back_kb("adm:panel", "◀️ Назад"),
        )
        return
    link = build_claim_link(bot_username, client.sub_id, cfg.claim_secret, ttl=cfg.claim_ttl_hours * 3600)
    text = (
        "🔗 <b>Ссылка для привязки</b>\n\n"
        f"Клиент: <code>{esc(client.email)}</code>\n"
        f"Срок в панели: <b>{fmt_dt(client.expires_at)}</b>\n"
        f"Ссылка действует: <b>{cfg.claim_ttl_hours} ч.</b>\n\n"
        f"<code>{esc(link)}</code>\n\n"
        "Передайте клиенту ссылку или QR: он открывает бота — подписка привязывается к его Telegram."
    )
    await message.answer(text, reply_markup=back_kb("adm:panel", "◀️ Назад"), disable_web_page_preview=True)
    png = make_qr_png(link)
    if png:
        await message.answer_photo(BufferedInputFile(png, filename="claim-qr.png"),
                                   caption="📷 QR для привязки подписки")
    else:
        await message.answer("ℹ️ Модуль qrcode не установлен — используйте ссылку выше.")


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
async def _render_users(call: CallbackQuery, db: Database, cfg: Config, page: int) -> None:
    total = await db.count_users()
    if total == 0:
        await call.answer("Пользователей пока нет.", show_alert=True)
        return
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = await db.users_page(PAGE_SIZE, page * PAGE_SIZE)
    lines = [f"👥 <b>Пользователи</b> ({total}, стр. {page + 1}/{total_pages})\n"]
    for row in rows:
        name = row["full_name"] or (f"@{row['username']}" if row["username"] else "без имени")
        marks = []
        if row["is_banned"]:
            marks.append("🚫")
        if row["subs"]:
            marks.append("🟢")
        entry = f"{' '.join(marks)} <code>{row['telegram_id']}</code> {esc(name)}"
        lines.append(entry)
    lines.append("\nНажмите на ID в списке ниже, чтобы открыть карточку.")
    ids = [int(row["telegram_id"]) for row in rows]
    buttons = [[InlineKeyboardButton(text=str(uid), callback_data=f"adm:user:card:{uid}")] for uid in ids]
    buttons.append([])
    keyboard_rows = buttons + [
        [
            InlineKeyboardButton(text="◀️", callback_data=f"adm:users:page:{page - 1}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"adm:users:page:{page + 1}"),
        ] if total_pages > 1 else [],
        [InlineKeyboardButton(text="🔍 Найти по ID", callback_data="adm:users:find")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:panel")],
    ]
    keyboard_rows = [row for row in keyboard_rows if row]
    await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))


@router.callback_query(F.data == "adm:users")
async def cb_users(call: CallbackQuery, db: Database, cfg: Config) -> None:
    await _render_users(call, db, cfg, 0)
    await call.answer()


@router.callback_query(F.data.startswith("adm:users:page:"))
async def cb_users_page(call: CallbackQuery, db: Database, cfg: Config) -> None:
    page = int(call.data.rsplit(":", 1)[1])
    await _render_users(call, db, cfg, page)
    await call.answer()


@router.callback_query(F.data == "adm:users:find")
async def cb_users_find(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.user_find)
    await call.message.answer("🔍 Отправьте Telegram ID, @username или номер заказа:", reply_markup=cancel_kb("adm:users"))
    await call.answer()


@router.message(AdminStates.user_find)
async def msg_users_find(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    query = (message.text or "").strip()
    await state.clear()
    row = None
    if query.isdigit():
        if len(query) > 6:
            row = await db.get_user(int(query))
        else:
            order = await db.get_order(int(query))
            if order:
                row = await db.get_user(int(order["user_id"]))
    if row is None and query.startswith("@"):
        row = await db.find_user_by_username(query[1:])
    if row is None:
        await message.answer("Пользователь не найден.", reply_markup=back_kb("adm:users", "◀️ К списку"))
        return
    await _show_user_card(message, db, cfg, int(row["telegram_id"]))


async def _show_user_card(target: Message | CallbackQuery, db: Database, cfg: Config, user_id: int) -> None:
    user = await db.get_user(user_id)
    if user is None:
        text = "Пользователь не найден."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)
        return
    subs = await db.user_subscriptions(user_id)
    orders = await db.user_orders(user_id, limit=5)
    active = [s for s in subs if s["status"] == "active"]
    text = (
        f"👤 <b>{esc(user['full_name'] or 'без имени')}</b>\n"
        f"ID: <code>{user_id}</code> | @{esc(user['username'] or '—')}\n"
        f"Статус: {'🚫 заблокирован' if user['is_banned'] else '✅ активен'}\n"
        f"Регистрация: {fmt_dt(user['created_at'])}, последняя активность: {fmt_dt(user['last_seen_at'])}\n"
    )
    if active:
        sub = active[0]
        text += (
            f"\n🟢 Подписка: <b>{esc(sub['plan_code'])}</b>, до {fmt_dt(sub['expires_at'])} "
            f"(осталось {human_left(sub['expires_at'])}), панель <code>{esc(sub['panel'])}</code>\n"
        )
    else:
        text += "\n🔴 Активной подписки нет\n"
    if orders:
        text += "\n🧾 Последние заказы:\n"
        for order in orders:
            text += (
                f"#{order['id']} · {esc(order['plan_code'])} · {price(int(order['amount']), cfg.currency)} · "
                f"{STATUS_RU.get(order['status'], order['status'])}\n"
            )
    markup = admin_user_kb(user_id, bool(user["is_banned"]))
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except Exception:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:user:card:"))
async def cb_user_card(call: CallbackQuery, db: Database, cfg: Config) -> None:
    user_id = int(call.data.rsplit(":", 1)[1])
    await _show_user_card(call, db, cfg, user_id)
    await call.answer()


@router.callback_query(F.data.startswith("adm:user:"))
async def cb_user_action(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    parts = call.data.split(":")
    action = parts[2]
    user_id = int(parts[3])
    user = await db.get_user(user_id)
    if user is None:
        await call.answer("Пользователь не найден.", show_alert=True)
        return

    if action == "ban":
        await db.set_user_field(user_id, "is_banned", 1)
        await call.answer("Пользователь заблокирован", show_alert=True)
        await _show_user_card(call, db, cfg, user_id)
        return
    if action == "unban":
        await db.set_user_field(user_id, "is_banned", 0)
        await call.answer("Пользователь разблокирован", show_alert=True)
        await _show_user_card(call, db, cfg, user_id)
        return
    if action == "msg":
        await state.update_data(target_user=user_id)
        await state.set_state(AdminStates.user_message)
        await call.message.answer(f"✉️ Введите сообщение для <code>{user_id}</code>:", reply_markup=cancel_kb("adm:panel"))
        await call.answer()
        return
    if action == "grant":
        plans = await db.all_plans()
        rows = [[InlineKeyboardButton(text=f"{plan['title']} ({plan['days']} дн.)",
                                      callback_data=f"adm:grantplan:{user_id}:{plan['code']}")]
                for plan in plans]
        rows.append([InlineKeyboardButton(text="🎁 Пробный период", callback_data=f"adm:grantplan:{user_id}:trial")])
        rows.append([InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:panel")])
        await call.message.answer(f"🎁 Какую подписку выдать <code>{user_id}</code>?",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await call.answer()
        return
    if action == "wipe":
        panel = get_panel(cfg, db)
        removed = 0
        for sub in await db.user_subscriptions(user_id):
            if sub["status"] == "active":
                try:
                    await panel.delete_client(panel_username=sub["panel_username"] or "")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не удалось удалить клиента %s: %s", sub["panel_username"], exc)
            await db.update_subscription(int(sub["id"]), status="disabled")
            removed += 1
        await call.answer(f"Отключено подписок: {removed}", show_alert=True)
        await _show_user_card(call, db, cfg, user_id)
        return
    await call.answer()


@router.callback_query(F.data.startswith("adm:grantplan:"))
async def cb_grant_plan(call: CallbackQuery, db: Database, cfg: Config, bot: Bot) -> None:
    _, _, raw_id, plan_code = call.data.split(":", 3)
    user_id = int(raw_id)
    try:
        await grant_subscription(bot, cfg, db, user_id, plan_code)
    except PanelError as exc:
        await call.answer(f"Ошибка выдачи: {exc}", show_alert=True)
        return
    await call.message.edit_text(f"✅ Подписка <b>{plan_code}</b> выдана пользователю <code>{user_id}</code>.")
    await call.answer()


@router.message(AdminStates.user_message)
async def msg_user_message(message: Message, state: FSMContext, bot: Bot, cfg: Config) -> None:
    data = await state.get_data()
    user_id = int(data.get("target_user") or 0)
    await state.clear()
    delivered = await safe_send(bot, user_id, f"✉️ <b>Сообщение от администратора</b>\n\n{esc(message.text or '')}")
    await message.answer("✅ Отправлено." if delivered else "⚠️ Пользователь недоступен (заблокировал бота).",
                         reply_markup=back_kb("adm:panel", "◀️ Назад"))


# ---------------------------------------------------------------------------
# Промокоды
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:promos")
async def cb_promos(call: CallbackQuery, db: Database) -> None:
    promos = await db.all_promos()
    text = f"🎟 <b>Промокоды</b>\n\nВсего: {len(promos)}"
    if promos:
        text += "\nНажмите на промокод ниже, чтобы включить/выключить или удалить."
    keyboard_rows = []
    for promo in promos[:15]:
        state = "🟢" if promo["is_active"] else "🔴"
        value = f"{promo['value']}%" if promo["discount_type"] == "percent" else f"{promo['value']}₽"
        uses = f"{promo['used']}/{promo['max_uses'] or '∞'}"
        keyboard_rows.append([InlineKeyboardButton(
            text=f"{state} {promo['code']} — {value}, {uses}",
            callback_data=f"adm:promo:card:{promo['code']}",
        )])
    keyboard_rows.append([InlineKeyboardButton(text="➕ Создать промокод", callback_data="adm:promo:new")])
    keyboard_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:panel")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await call.answer()


@router.callback_query(F.data.startswith("adm:promo:card:"))
async def cb_promo_card(call: CallbackQuery, db: Database) -> None:
    code = call.data.rsplit(":", 1)[1]
    promo = await db.get_promo(code)
    if promo is None:
        await call.answer("Промокод не найден.", show_alert=True)
        return
    value = f"{promo['value']}%" if promo["discount_type"] == "percent" else f"{price(int(promo['value']))}"
    text = (
        f"🎟 <b>Промокод {esc(promo['code'])}</b>\n\n"
        f"Скидка: <b>{value}</b>\n"
        f"Активаций: {promo['used']} из {promo['max_uses'] or '∞'}\n"
        f"Действует до: {fmt_dt(promo['expires_at']) if promo['expires_at'] else 'бессрочно'}\n"
        f"Статус: {'🟢 активен' if promo['is_active'] else '🔴 выключен'}"
    )
    keyboard_rows = [
        [InlineKeyboardButton(text="🔴 Выключить" if promo["is_active"] else "🟢 Включить",
                              callback_data=f"adm:promo:toggle:{promo['code']}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:promo:del:{promo['code']}")],
        [InlineKeyboardButton(text="◀️ К промокодам", callback_data="adm:promos")],
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await call.answer()


@router.callback_query(F.data.startswith("adm:promo:toggle:"))
async def cb_promo_toggle(call: CallbackQuery, db: Database) -> None:
    code = call.data.rsplit(":", 1)[1]
    promo = await db.get_promo(code)
    if promo is None:
        await call.answer("Промокод не найден.", show_alert=True)
        return
    await db.toggle_promo(code, not bool(promo["is_active"]))
    await call.answer("Готово")
    await cb_promos(call, db)


@router.callback_query(F.data.startswith("adm:promo:del:"))
async def cb_promo_delete(call: CallbackQuery, db: Database) -> None:
    await db.delete_promo(call.data.rsplit(":", 1)[1])
    await call.answer("Удалён")
    await cb_promos(call, db)


@router.callback_query(F.data == "adm:promo:list")
async def cb_promo_list(call: CallbackQuery, db: Database) -> None:
    await cb_promos(call, db)


@router.callback_query(F.data == "adm:promo:new")
async def cb_promo_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.promo_code)
    await call.message.answer("🎟 Введите код промокода (латиница/цифры), например <code>VPN10</code>:",
                              reply_markup=cancel_kb("adm:promos"))
    await call.answer()


@router.message(AdminStates.promo_code)
async def msg_promo_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    if not code or len(code) > 32:
        await message.answer("Код должен быть от 1 до 32 символов.")
        return
    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.promo_type)
    await message.answer(
        "Тип скидки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Процент (%)", callback_data="adm:promo:type:percent")],
            [InlineKeyboardButton(text="Фиксированная сумма (₽)", callback_data="adm:promo:type:fixed")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm:promos")],
        ]),
    )


@router.callback_query(F.data.startswith("adm:promo:type:"))
async def cb_promo_type(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(promo_type=call.data.rsplit(":", 1)[1])
    await state.set_state(AdminStates.promo_value)
    await call.message.answer("Введите размер скидки (например 10 для 10% или 200 для скидки 200₽):",
                              reply_markup=cancel_kb("adm:promos"))
    await call.answer()


@router.message(AdminStates.promo_value)
async def msg_promo_value(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Введите положительное число.")
        return
    await state.update_data(promo_value=int(raw))
    await state.set_state(AdminStates.promo_uses)
    await message.answer("Сколько раз промокод можно активировать? (0 = без ограничения)")


@router.message(AdminStates.promo_uses)
async def msg_promo_uses(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите число (0 = без ограничения).")
        return
    await state.update_data(promo_uses=int(raw))
    await state.set_state(AdminStates.promo_days)
    await message.answer("На сколько дней действует промокод? (0 = бессрочно)")


@router.message(AdminStates.promo_days)
async def msg_promo_days(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите число дней (0 = бессрочно).")
        return
    data = await state.get_data()
    await state.clear()
    expires = to_iso(utcnow() + timedelta(days=int(raw))) if int(raw) > 0 else None
    await db.create_promo(data["promo_code"], data.get("promo_type", "percent"),
                          int(data["promo_value"]), int(data.get("promo_uses", 0)), expires)
    promo = await db.get_promo(data["promo_code"])
    value = f"{promo['value']}%" if promo["discount_type"] == "percent" else price(int(promo["value"]), cfg.currency)
    await message.answer(
        f"✅ Промокод <code>{esc(promo['code'])}</code> создан.\n"
        f"Скидка: <b>{value}</b>, активаций: {promo['max_uses'] or '∞'}, "
        f"до: {fmt_dt(promo['expires_at']) if promo['expires_at'] else 'бессрочно'}",
        reply_markup=back_kb("adm:promos", "◀️ К промокодам"),
    )


# ---------------------------------------------------------------------------
# Рассылка
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast_text)
    await call.message.answer(
        "📢 Отправьте текст рассылки (поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "ссылки).",
        reply_markup=cancel_kb("adm:panel"),
    )
    await call.answer()


@router.message(AdminStates.broadcast_text)
async def msg_broadcast_text(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.html_text or message.text or "")
    await state.set_state(None)
    await message.answer("Кому отправить?", reply_markup=broadcast_audience_kb())


@router.callback_query(F.data.startswith("adm:bcast:"))
async def cb_broadcast_send(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    audience = call.data.rsplit(":", 1)[1]
    data = await state.get_data()
    text = data.get("broadcast_text")
    await state.clear()
    if not text:
        await call.answer("Текст рассылки потерян, начните заново.", show_alert=True)
        return
    ids = await db.all_user_ids(audience)
    await call.message.edit_text(f"📢 Отправляю… получателей: {len(ids)}")
    await call.answer()

    sent = failed = 0
    for batch in chunks(ids, 25):
        import asyncio

        results = await asyncio.gather(
            *(safe_send(bot, uid, text) for uid in batch), return_exceptions=True
        )
        for result in results:
            if result is True:
                sent += 1
            else:
                failed += 1
        await asyncio.sleep(1.0)  # не упираемся в лимиты Telegram

    await db.log_broadcast(call.from_user.id, text, audience, sent, failed)
    await call.message.answer(
        f"✅ Рассылка завершена.\nДоставлено: <b>{sent}</b>, ошибок: {failed}",
        reply_markup=back_kb("adm:panel", "◀️ Назад"),
    )


# ---------------------------------------------------------------------------
# Тикеты
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:tickets")
async def cb_tickets(call: CallbackQuery, db: Database) -> None:
    tickets = await db.open_tickets()
    if not tickets:
        await call.message.edit_text("🧾 <b>Тикеты</b>\n\nОткрытых обращений нет.",
                                     reply_markup=back_kb("adm:panel", "◀️ Назад"))
        await call.answer()
        return
    await call.message.edit_text(f"🧾 <b>Открытые обращения</b>: {len(tickets)}",
                                 reply_markup=back_kb("adm:panel", "◀️ Назад"))
    for ticket in tickets:
        messages = await db.ticket_messages(int(ticket["id"]), limit=5)
        body = "\n".join(
            f"{'👨‍💼' if m['from_admin'] else '👤'} {esc((m['text'] or '')[:200])}"
            for m in reversed(messages)
        ) or "—"
        await call.message.answer(
            f"🧾 <b>Обращение #{ticket['id']}</b>\n"
            f"Пользователь: <code>{ticket['user_id']}</code>\n"
            f"Тема: {esc(ticket['subject'])}\n"
            f"Обновлено: {fmt_dt(ticket['updated_at'])}\n\n{body}",
            reply_markup=admin_ticket_kb(int(ticket["id"])),
        )
    await call.answer()


@router.callback_query(F.data.startswith("adm:ticket:"))
async def cb_ticket_action(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    _, _, action, raw_id = call.data.split(":", 3)
    ticket_id = int(raw_id)
    ticket = await db.get_ticket(ticket_id)
    if ticket is None:
        await call.answer("Обращение не найдено.", show_alert=True)
        return
    if action == "reply":
        await state.update_data(ticket_id=ticket_id)
        await state.set_state(AdminStates.ticket_reply)
        await call.message.answer(f"✉️ Ответ для обращения #{ticket_id}:", reply_markup=cancel_kb("adm:tickets"))
        await call.answer()
        return
    await db.close_ticket(ticket_id)
    await safe_send(bot, int(ticket["user_id"]), f"✅ Ваше обращение #{ticket_id} закрыто. Спасибо за обращение!")
    await call.message.edit_text(f"✅ Обращение #{ticket_id} закрыто.")
    await call.answer()


@router.message(AdminStates.ticket_reply)
async def msg_ticket_reply(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id") or 0)
    await state.clear()
    ticket = await db.get_ticket(ticket_id)
    if ticket is None:
        await message.answer("Обращение не найдено.", reply_markup=back_kb("adm:tickets"))
        return
    text = message.html_text or message.text or ""
    await db.add_ticket_message(ticket_id, True, text)
    delivered = await safe_send(bot, int(ticket["user_id"]),
                                f"💬 <b>Ответ поддержки</b> (обращение #{ticket_id})\n\n{text}")
    await message.answer("✅ Ответ отправлен." if delivered else "⚠️ Пользователь недоступен.",
                         reply_markup=back_kb("adm:tickets", "◀️ К тикетам"))


# ---------------------------------------------------------------------------
# Настройки: тарифы, пробный период
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:settings")
async def cb_settings(call: CallbackQuery, db: Database, cfg: Config) -> None:
    plans = await db.all_plans()
    text = (
        "🎛 <b>Настройки</b>\n\n"
        f"Панель выдачи: <code>{cfg.panel}</code>\n"
        f"Способы оплаты: <code>{', '.join(cfg.payment_methods)}</code>\n"
        f"Пробный период: {'включён, ' + str(rt.trial_days) + ' дн.' if rt.trial_enabled else 'выключен'}\n"
        f"Тарифов: {len(plans)}\n\n"
        "Значение пробного периода меняется на время работы бота "
        "(постоянное значение задаётся в .env)."
    )
    await call.message.edit_text(text, reply_markup=settings_kb())
    await call.answer()


@router.callback_query(F.data == "adm:settings:plans")
async def cb_settings_plans(call: CallbackQuery, db: Database) -> None:
    plans = await db.all_plans()
    text = "📦 <b>Тарифы</b>\n\nВыберите тариф для редактирования.\nИзменения применяются сразу."
    await call.message.edit_text(text, reply_markup=plans_admin_kb(plans))
    await call.answer()


@router.callback_query(F.data.startswith("adm:plan:"))
async def cb_plan_edit(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config) -> None:
    parts = call.data.split(":")
    if len(parts) == 3:
        code = parts[2]
        plan = await db.get_plan(code)
        if plan is None:
            await call.answer("Тариф не найден.", show_alert=True)
            return
        await call.message.edit_text(
            f"📦 <b>{esc(plan['title'])}</b> (<code>{code}</code>)\n\n"
            f"Цена: <b>{price(int(plan['price']), cfg.currency)}</b>\n"
            f"Срок: <b>{plan['days']} дн.</b>\n"
            f"Трафик: <b>{plan['traffic_gb'] or 'безлимит'}</b> ГБ\n"
            f"Устройств: <b>{plan['devices']}</b>\n"
            f"Статус: {'🟢 показан' if plan['is_active'] else '🔴 скрыт'}",
            reply_markup=plan_edit_kb(code, int(plan["is_active"])),
        )
        await call.answer()
        return

    _, _, field, code = parts[0], parts[1], parts[2], parts[3]
    plan = await db.get_plan(code)
    if plan is None:
        await call.answer("Тариф не найден.", show_alert=True)
        return
    if field == "toggle":
        await db.upsert_plan(code, plan["title"], int(plan["price"]), int(plan["days"]),
                             int(plan["traffic_gb"]), int(plan["devices"]),
                             is_active=0 if plan["is_active"] else 1)
        await call.answer("Статус изменён")
        await cb_settings_plans(call, db)
        return
    prompts = {
        "title": "🏷 Введите новое название тарифа:",
        "price": "💵 Введите новую цену в рублях (целое число):",
        "days": "📅 Введите срок действия в днях:",
        "traffic": "📶 Введите лимит трафика в ГБ (0 = безлимит):",
        "devices": "👥 Введите лимит устройств:",
    }
    await state.update_data(plan_code=code, plan_field=field)
    await state.set_state(AdminStates.plan_value)
    await call.message.answer(prompts.get(field, "Введите значение:"), reply_markup=cancel_kb("adm:settings:plans"))
    await call.answer()


@router.message(AdminStates.plan_value)
async def msg_plan_value(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    data = await state.get_data()
    await state.clear()
    code, field = data.get("plan_code"), data.get("plan_field")
    plan = await db.get_plan(code) if code else None
    if plan is None or not field:
        await message.answer("Тариф не найден.", reply_markup=back_kb("adm:settings:plans"))
        return
    raw = (message.text or "").strip()
    if field == "title":
        await db.upsert_plan(code, raw, int(plan["price"]), int(plan["days"]),
                             int(plan["traffic_gb"]), int(plan["devices"]))
        await message.answer("✅ Сохранено.", reply_markup=back_kb("adm:settings:plans", "◀️ К тарифам"))
        return
    if not raw.isdigit():
        await message.answer("Введите целое число.", reply_markup=back_kb("adm:settings:plans"))
        return
    value = int(raw)
    values = {
        "price": (value, int(plan["days"]), int(plan["traffic_gb"]), int(plan["devices"])),
        "days": (int(plan["price"]), max(1, value), int(plan["traffic_gb"]), int(plan["devices"])),
        "traffic": (int(plan["price"]), int(plan["days"]), value, int(plan["devices"])),
        "devices": (int(plan["price"]), int(plan["days"]), int(plan["traffic_gb"]), max(1, value)),
    }[field]
    await db.upsert_plan(code, plan["title"], *values)
    await message.answer("✅ Сохранено.", reply_markup=back_kb("adm:settings:plans", "◀️ К тарифам"))


@router.callback_query(F.data == "adm:settings:trial")
async def cb_trial(call: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    await state.set_state(AdminStates.trial_days)
    await call.message.answer(
        f"Сейчас пробный период: {'включён, ' + str(rt.trial_days) + ' дн.' if rt.trial_enabled else 'выключен'}.\n"
        "Введите количество дней (0 = выключить).\n"
        "Чтобы изменить навсегда — правьте TRIAL_ENABLED/TRIAL_DAYS в <code>.env</code>.",
        reply_markup=cancel_kb("adm:settings"),
    )
    await call.answer()


@router.message(AdminStates.trial_days)
async def msg_trial_days(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите число дней (0 = выключить).", reply_markup=back_kb("adm:settings"))
        return
    await rt.set_trial(db, int(raw))
    await message.answer(
        f"✅ Пробный период: {'выключен' if raw == '0' else raw + ' дн.'}",
        reply_markup=back_kb("adm:settings", "◀️ В настройки"),
    )


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:users:find:noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, cfg: Config) -> None:
    stats = await db.stats()
    await message.answer(
        f"📊 Пользователей: {stats['users_total']}, подписок: {stats['subs_active']}, "
        f"выручка: {price(stats['revenue_total'], cfg.currency)}",
        reply_markup=main_menu(True, rt.trial_enabled),
    )
