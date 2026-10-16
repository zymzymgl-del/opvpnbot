"""Хендлеры пользовательской части: магазин, оплата, ключи, профиль, поддержка."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import Config
from app.db import Database, from_iso
from app.keyboards import (
    BTN_ADMIN,
    BTN_BUY,
    BTN_HELP,
    BTN_MY_KEYS,
    BTN_PROFILE,
    BTN_SUPPORT,
    BTN_TRIAL,
    MAIN_MENU_TEXT,
    back_kb,
    cancel_kb,
    checkout_kb,
    keys_kb,
    main_menu,
    payment_methods_kb,
    plans_kb,
    profile_kb,
    subscription_kb,
    support_kb,
    switch_plans_kb,
)
from app import ui
from app.runtime import runtime as rt
from app.services.panels import PanelError
from app.services.payments import PaymentError
from app.services.subs import (
    complete_order,
    grant_subscription,
    notify_admins,
    payment_ids_of,
    renewal_available,
    renewal_window_days,
    send_subscription_card,
)
from app.utils import (
    METHOD_RU,
    STATUS_RU,
    apps_instructions,
    esc,
    fmt_dt,
    human_left,
    make_qr_png,
    pick_link,
    price,
)

log = logging.getLogger(__name__)
router = Router(name="user")

FAQ_TEXT = (
    "❓ <b>Частые вопросы</b>\n\n"
    "<b>Как подключиться?</b>\n"
    "«🔑 Моя подписка» → выберите подписку → «📲 Как подключиться». Импортируйте ссылку в приложение "
    "(v2rayNG, Hiddify, Streisand, NekoBox) и нажмите «Подключиться».\n\n"
    "<b>Интернет не работает после подключения?</b>\n"
    "Проверьте, что приложение показывает «Подключено», перезапустите приложение, "
    "попробуйте другой сервер в списке. Если не помогло — напишите в поддержку.\n\n"
    "<b>Можно ли на несколько устройств?</b>\n"
    "Да, в рамках лимита устройств вашего тарифа (указан в карточке подписки).\n\n"
    "<b>Как продлить?</b>\n"
    "«🔑 Моя подписка» → подписка → «🔄 Продлить». Конфиг остаётся тем же, срок продлевается.\n\n"
    "<b>Оплата не прошла, деньги списались?</b>\n"
    "Напишите в поддержку — проверим платёж и вернём деньги либо выдадим доступ вручную."
)


def _methods_line(cfg: Config) -> str:
    """Человеческий список включённых способов оплаты (без служебного «none»)."""
    names = [METHOD_RU[m] for m in cfg.payment_methods if m in METHOD_RU]
    return ", ".join(names) if names else "по согласованию с поддержкой"


async def _edit_or_answer(
    call: CallbackQuery,
    text: str,
    markup=None,
) -> None:
    """Правит текущее сообщение (режим одного окна), иначе шлёт новое."""
    await ui.ui_text_from_message(call.bot, call.message, text, markup)


async def _working(call: CallbackQuery, text: str, markup=None) -> None:
    """Промежуточный статус («⏳ делаю…») в окне перед долгой работой.

    Спиннер на кнопке гаснет сразу — ответ на нажатие отправляется первым же
    действием. Но дальше идут запросы в ЮKassa (до 15 с на счёт), выдача в
    панели 3x-ui (до 20 с) и отправка файлов, поэтому пользователю нужен
    видимый прогресс, а не «замерший» экран.
    Кнопка возврата в разметке обязательна: если хендлер упадёт после этого
    шага, окно не останется без выхода.
    """
    await _edit_or_answer(call, text, back_kb() if markup is None else markup)


async def _show_main_menu(call: CallbackQuery, cfg: Config) -> None:
    await _edit_or_answer(call, MAIN_MENU_TEXT, main_menu(cfg.is_admin(call.from_user.id), rt.trial_enabled))


async def _send_menu_window(bot: Bot, message: Message, text: str, cfg: Config) -> None:
    """Показывает окно меню с инлайн-кнопками в едином окне чата."""
    await ui.ui_text(
        bot, message.chat.id, text,
        main_menu(cfg.is_admin(message.from_user.id), rt.trial_enabled))


#: Ключ в FSM-данных: id сообщения-«окна», которое правят шаги ввода текста.
UI_WINDOW_KEY = "ui_window_id"


async def _remember_window(state: FSMContext, message_id: int) -> None:
    """Запоминает окно, которое будут править последующие шаги ввода текста."""
    await state.update_data(**{UI_WINDOW_KEY: message_id})


async def _prompt_window(call: CallbackQuery, state: FSMContext, text: str, markup=None) -> None:
    """Показывает подсказку ввода в текущем окне (режим одного окна)."""
    await _remember_window(state, call.message.message_id)
    await _edit_or_answer(call, text, markup)


async def _apply_window(bot: Bot, chat_id: int, window_id, text: str, markup=None) -> None:
    """Результат ввода: правит окно (если помним его id), иначе шлёт новое сообщение."""
    if isinstance(window_id, int):
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=window_id, reply_markup=markup)
            ui.remember(chat_id, window_id)
            return
        except Exception as exc:  # окно могло быть удалено — не критично
            log.info("Не удалось обновить окно %s: %s", window_id, exc)
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    if getattr(sent, "message_id", None):
        ui.remember(chat_id, sent.message_id)


class PromoStates(StatesGroup):
    enter = State()


class TicketStates(StatesGroup):
    subject = State()
    message = State()
    reply = State()


# ---------------------------------------------------------------------------
# Старт и главное меню
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, cfg: Config, db: Database, bot: Bot, bot_username: str = "") -> None:
    user = message.from_user
    # /start — явная команда «с чистого листа»: забываем старое окно,
    # чтобы меню было отправлено новым сообщением, а не отредактировано в старом.
    ui.forget(int(message.chat.id))
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1].strip()

    if payload.startswith("claim_"):
        from app.services.claims import CLAIM_PREFIX, bind_claim

        # Привязка ищет клиента в панели (до 20 с) — показываем статус, чтобы
        # пользователь не ждал молча; карточка/QR заменят это же окно.
        await ui.ui_text(bot, message.chat.id, "⏳ Проверяю ваш конфиг в панели…",
                         back_kb("menu:main", "🏠 В меню"))
        result = await bind_claim(bot, cfg, db, user.id, payload[len(CLAIM_PREFIX):])
        prefix = "🎉 " if result.ok else "⚠️ "
        await _send_menu_window(bot, message, prefix + esc(result.message), cfg)
        return

    tail = "Все покупки — в «🔑 Моя подписка», профиль — в «👤 Профиль», вопросы — «🆘 Поддержка». Меню ниже 👇"
    greeting = (
        f"👋 <b>Добро пожаловать в {esc(cfg.bot_name)}!</b>\n\n"
        "Как это работает — 3 шага:\n"
        "1️⃣ «🛒 Купить VPN» → выберите тариф;\n"
        f"2️⃣ оплатите удобным способом ({_methods_line(cfg)});\n"
        "3️⃣ получите ключ, QR и инструкцию — сразу после оплаты.\n\n"
        f"{tail}"
    )
    sub = await db.active_subscription(user.id)
    if sub is not None:
        greeting += f"\n🟢 Ваша подписка активна, осталось <b>{human_left(sub['expires_at'])}</b>."
    await _send_menu_window(bot, message, greeting, cfg)


@router.message(Command("menu"))
async def cmd_menu(message: Message, cfg: Config, bot: Bot) -> None:
    await _send_menu_window(bot, message, MAIN_MENU_TEXT, cfg)


#: Тексты кнопок старой reply-клавиатуры — для бесшовного перехода пользователей.
LEGACY_REPLY_TEXTS = {BTN_BUY, BTN_MY_KEYS, BTN_PROFILE, BTN_SUPPORT, BTN_TRIAL, BTN_HELP, BTN_ADMIN}


@router.message(F.text.in_(LEGACY_REPLY_TEXTS))
async def msg_legacy_reply_button(message: Message, cfg: Config, bot: Bot) -> None:
    """Нажатие кнопки со старой reply-клавиатуры: показываем инлайн-меню в окне."""
    await _send_menu_window(bot, message, MAIN_MENU_TEXT, cfg)


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(call: CallbackQuery, cfg: Config, state: FSMContext) -> None:
    await state.clear()
    await _show_main_menu(call, cfg)
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data == "check:subscription")
async def cb_check_subscription(call: CallbackQuery, cfg: Config, bot: Bot) -> None:
    if not cfg.required_channels:
        await call.answer("Подписка не требуется 🙂", show_alert=False)
        return
    for channel in cfg.required_channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=call.from_user.id)
        except Exception:
            continue
        if member.status not in {"creator", "administrator", "member", "restricted"}:
            await call.answer("Вы ещё не подписались на все каналы 🙂", show_alert=True)
            return
    await _show_main_menu(call, cfg)
    await call.answer("Готово! ✅")


# ---------------------------------------------------------------------------
# Магазин: тарифы и оформление заказа
# ---------------------------------------------------------------------------
async def _show_plans(target: Message | CallbackQuery, db: Database, cfg: Config, bot: Bot) -> None:
    user_id = target.from_user.id if target.from_user else 0
    existing = await db.active_subscription(user_id) if user_id else None
    if existing is not None and not renewal_available(existing, cfg):
        window = renewal_window_days(cfg)
        opens_at = from_iso(existing["expires_at"]) - timedelta(days=window)
        text = (
            "⏳ <b>Продление пока недоступно</b>\n\n"
            f"Продлить подписку можно за <b>{window} дн.</b> до её окончания — "
            "мы обязательно напомним.\n\n"
            f"Сейчас осталось: <b>{human_left(existing['expires_at'])}</b> "
            f"(до {fmt_dt(existing['expires_at'])}).\n"
            f"Окно продления откроется <b>{fmt_dt(opens_at)}</b>."
        )
        if isinstance(target, CallbackQuery):
            await _edit_or_answer(target, text)
        else:
            await ui.ui_text(bot, target.chat.id, text)
        return
    plans = await db.active_plans()
    if not plans:
        text = "😔 Сейчас нет доступных тарифов. Загляните позже или напишите в поддержку."
        if isinstance(target, CallbackQuery):
            await _edit_or_answer(target, text)
        else:
            await ui.ui_text(bot, target.chat.id, text)
        return
    text = (
        "🛒 <b>Выберите тариф</b>\n\n"
        f"Оплата: {_methods_line(cfg)}.\n"
        f"Пробный период: {'да, ' + str(rt.trial_days) + ' дн.' if rt.trial_enabled else 'нет'}\n\n\n"

    )
    markup = plans_kb(plans, cfg.currency)
    if isinstance(target, CallbackQuery):
        await _edit_or_answer(target, text, markup)
    else:
        await ui.ui_text(bot, target.chat.id, text, markup)


@router.callback_query(F.data == "menu:buy")
async def cb_plans(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext) -> None:
    await state.clear()  # эта кнопка — цель «Отмены» шагов ввода
    # «Одно окно»: пока есть незакрытый заказ — показываем именно его, а не список
    # тарифов (иначе кажется, что можно открыть второй тариф и оплатить оба).
    order = await db.user_pending_order(call.from_user.id, "subscription")
    if order is not None:
        await _show_order(call, db, cfg, int(order["id"]))
        await call.answer()
        return
    await _show_plans(call, db, cfg, call.bot)
    await call.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery, db: Database, cfg: Config) -> None:
    code = call.data.split(":", 1)[1]
    plan = await db.get_plan(code)
    if plan is None or not plan["is_active"]:
        await call.answer("Этот тариф недоступен.", show_alert=True)
        return
    existing = await db.active_subscription(call.from_user.id)
    if existing is not None and not renewal_available(existing, cfg):
        await call.answer("Продление откроется ближе к окончанию подписки.", show_alert=True)
        return
    reuse = await db.pending_order(call.from_user.id, code, "subscription")
    if reuse is not None:
        order_id = int(reuse["id"])
    else:
        # Один незакрытый заказ на пользователя: пока есть неоплаченный заказ на
        # ДРУГОЙ тариф, новый не создаём — иначе можно «наплодить» счёта и
        # оплатить не тот (деньги списываются, период не выдаётся).
        other = await db.user_pending_order(call.from_user.id, "subscription")
        if other is not None:
            await _show_order(call, db, cfg, int(other["id"]))
            await call.answer(
                "⚠️ У вас уже есть неоплаченный заказ. Оплатите или отмените его, затем оформите новый.",
                show_alert=True,
            )
            return
        # Анти-абьюз: не даём копить неоплаченные счета (защита от спама заказами).
        limit = cfg.max_pending_orders
        if limit > 0:
            pending = await db.count_pending_orders(call.from_user.id)
            if pending >= limit:
                await call.answer(
                    f"⚠️ У вас уже {pending} неоплаченных заказов. "
                    "Оплатите или отмените их (в истории заказов), затем оформите новый.",
                    show_alert=True,
                )
                return
        order_id = await db.create_order(
            user_id=call.from_user.id, plan_code=code, amount=int(plan["price"]),
            base_amount=int(plan["price"]), promo_code=None, method="none", kind="subscription",
        )
    await _show_order(call, db, cfg, order_id)
    await call.answer()


async def _show_order(target: CallbackQuery, db: Database, cfg: Config, order_id: int) -> None:
    order = await db.get_order(order_id)
    if order is None:
        await target.answer("Заказ не найден.", show_alert=True)
        return
    plan = await db.get_plan(order["plan_code"])
    text = (
        f"🧾 <b>Заказ #{order_id}</b>\n\n"
        f"Тариф: <b>{esc(plan['title'] if plan else order['plan_code'])}</b>\n"
        f"Срок: {plan['days'] if plan else '—'} дн.\n"
        f"Устройств: {plan['devices'] if plan else '—'}\n"
    )
    if order["discount"]:
        text += f"Скидка: <b>−{price(int(order['discount']), cfg.currency)}</b> (промокод <code>{esc(order['promo_code'])}</code>)\n"
        text += f"Было: {price(int(order['base_amount']), cfg.currency)}\n"
    text += f"\nК оплате: <b>{price(int(order['amount']), cfg.currency)}</b>"
    available = [m for m in cfg.payment_methods if m != "none"]
    markup = payment_methods_kb(available, order_id)
    await _edit_or_answer(target, text, markup)


async def _reissue_blocked(call: CallbackQuery, order, cfg: Config) -> bool:
    """Сумма уже зафиксирована счётом — менять тариф/промокод нельзя.

    Счёт выписан на конкретную сумму; её изменение (особенно вверх) ломает
    сверку оплаты: деньги могут списаться, а период не выdcаться. Поэтому
    просим сначала отменить заказ. Возвращает True, если действие заблокировано.
    """
    if not order["payment_id"]:
        return False
    await call.answer(
        f"Уже выставлен счёт на {price(int(order['amount']), cfg.currency)}. "
        "Отмените заказ, чтобы изменить тариф или сумму.",
        show_alert=True,
    )
    return True


@router.callback_query(F.data.startswith("order:plans:"))
async def cb_order_plans(call: CallbackQuery, db: Database, cfg: Config) -> None:
    """Список тарифов для смены тарифа внутри текущего заказа."""
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await db.get_order(order_id)
    if not _is_owner(order, call.from_user.id):
        await call.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] != "pending":
        await call.answer("Заказ уже неактуален.", show_alert=True)
        return
    if await _reissue_blocked(call, order, cfg):
        return
    plans = await db.active_plans()
    if not plans:
        await call.answer("Сейчас нет доступных тарифов.", show_alert=True)
        return
    text = ("🔁 <b>Смена тарифа</b>\n\n"
            "Выберите новый тариф — он заменит текущий в этом же заказе "
            "(второй заказ не создаётся).")
    markup = switch_plans_kb(plans, cfg.currency, order_id)
    await _edit_or_answer(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("switch:"))
async def cb_switch_plan(call: CallbackQuery, db: Database, cfg: Config) -> None:
    """Меняет тариф у единственного незакрытого заказа (второй заказ не создаём)."""
    code = call.data.split(":", 1)[1]
    plan = await db.get_plan(code)
    if plan is None or not plan["is_active"]:
        await call.answer("Этот тариф недоступен.", show_alert=True)
        return
    order = await db.user_pending_order(call.from_user.id, "subscription")
    if order is None:
        # Заказа нет (например, только что отменили) — создаём как при покупке.
        order_id = await db.create_order(
            user_id=call.from_user.id, plan_code=code, amount=int(plan["price"]),
            base_amount=int(plan["price"]), promo_code=None, method="none", kind="subscription",
        )
        await _show_order(call, db, cfg, order_id)
        await call.answer()
        return
    order_id = int(order["id"])
    if order["plan_code"] == code:
        await _show_order(call, db, cfg, order_id)
        await call.answer("Этот тариф уже выбран")
        return
    if await _reissue_blocked(call, order, cfg):
        return
    # Промокод переносим с пересчётом скидки от новой базовой цены.
    new_base = int(plan["price"])
    promo_code = order["promo_code"] if int(order["promo_reserved"] or 0) else None
    discount = 0
    if promo_code:
        promo = await db.get_promo(promo_code)
        if promo is not None:
            if promo["discount_type"] == "percent":
                discount = new_base * int(promo["value"]) // 100
            else:
                discount = min(int(promo["value"]), new_base)
        else:
            # Промокод пропал из БД — снимаем резерв заказа.
            await db.release_promo(promo_code, call.from_user.id)
            await db.clear_order_promo_reservation(order_id)
            promo_code = None
    await db.update_order_plan(order_id, code, new_base, discount, promo_code,
                               1 if promo_code else 0)
    await _show_order(call, db, cfg, order_id)
    await call.answer("Тариф изменён")


def _is_owner(order, user_id: int) -> bool:
    """Заказ принадлежит пользователю (защита от доступа к чужому заказу по id)."""
    return order is not None and int(order["user_id"]) == int(user_id)


def _payment_amount(payment: dict) -> float | None:
    """Сумма платежа ЮKassa в рублях (None, если объект неполный).

    Нужна, чтобы решить, можно ли переиспользовать уже выставленный счёт:
    счёт на другую сумму не подойдёт (после применения промокода).
    """
    try:
        return float(payment["amount"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


@router.callback_query(F.data.startswith("order:cancel:"))
async def cb_cancel_order(call: CallbackQuery, db: Database, cfg: Config) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await db.get_order(order_id)
    if not _is_owner(order, call.from_user.id):
        await call.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] != "pending":
        await _show_main_menu(call, cfg)
        await call.answer("Заказ уже неактуален.", show_alert=True)
        return
    if order["promo_code"] and int(order["promo_reserved"] or 0):
        await db.release_promo(order["promo_code"], call.from_user.id)
        await db.clear_order_promo_reservation(order_id)
    await db.set_order_status(order_id, "canceled")
    # Возвращаем пользователя в главное меню (раньше окно оставалось без кнопок — «тупик»).
    await _edit_or_answer(
        call,
        "❌ Заказ отменён. Если оплата уже ушла — напишите в поддержку.\n\n" + MAIN_MENU_TEXT,
        main_menu(cfg.is_admin(call.from_user.id), rt.trial_enabled),
    )
    await call.answer("Заказ отменён")


# --- промокоды ---
@router.callback_query(F.data == "promo:enter")
async def cb_promo_enter(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.enter)
    await _prompt_window(call, state, "🎟 Введите промокод одним сообщением:", cancel_kb("menu:buy"))
    await call.answer()


@router.callback_query(F.data.startswith("promo:order:"))
async def cb_promo_order(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await db.get_order(order_id)
    if not _is_owner(order, call.from_user.id):
        await call.answer("Заказ не найден.", show_alert=True)
        return
    await state.update_data(promo_order=order_id)
    await state.set_state(PromoStates.enter)
    await _prompt_window(call, state, "🎟 Введите промокод:", cancel_kb(f"order:show:{order_id}"))
    await call.answer()


@router.callback_query(F.data.startswith("order:show:"))
async def cb_order_show(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext) -> None:
    await state.clear()
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await db.get_order(order_id)
    if not _is_owner(order, call.from_user.id):
        await call.answer("Заказ не найден.", show_alert=True)
        return
    await _show_order(call, db, cfg, order_id)
    await call.answer()


@router.message(PromoStates.enter)
async def msg_promo(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    data = await state.get_data()
    order_id = data.get("promo_order")
    window_id = data.get(UI_WINDOW_KEY)
    code = (message.text or "").strip().upper()
    await state.clear()

    if not order_id:
        plans = await db.active_plans()
        await _apply_window(bot, message.chat.id, window_id,
                            "Промокод можно применить при оформлении заказа: выберите тариф и нажмите «🎟 Промокод».",
                            plans_kb(plans, cfg.currency))
        return

    order = await db.get_order(int(order_id))
    if not _is_owner(order, message.from_user.id):
        await _apply_window(bot, message.chat.id, window_id, "Заказ не найден.", back_kb())
        return
    if order["status"] != "pending":
        await _apply_window(bot, message.chat.id, window_id, "Заказ уже неактуален.", back_kb())
        return
    if order["payment_id"]:
        # Сумма зафиксирована счётом — менять её нельзя (риск недоплаты/потери
        # оплаты). Сначала отменяем заказ, затем оформляем заново.
        await _apply_window(
            bot, message.chat.id, window_id,
            f"Уже выставлен счёт на <b>{price(int(order['amount']), cfg.currency)}</b>. "
            "Отмените заказ, чтобы изменить сумму.",
            back_kb(f"order:show:{order_id}"),
        )
        return

    base = int(order["base_amount"])
    reserved_code = order["promo_code"] if int(order["promo_reserved"] or 0) else None

    if reserved_code == code:
        # Промокод уже зарезервирован за этим заказом — повторный ввод идемпотентен.
        discount = int(order["discount"])
        new_amount = int(order["amount"])
    else:
        valid, reason, promo = await db.reserve_promo(code, message.from_user.id)
        if not valid or promo is None:
            await _apply_window(bot, message.chat.id, window_id, f"❌ {reason}",
                                back_kb(f"order:show:{order_id}"))
            return
        if promo["discount_type"] == "percent":
            discount = base * int(promo["value"]) // 100
        else:
            discount = min(int(promo["value"]), base)
        new_amount = max(1, base - discount)
        if reserved_code:
            # На заказе был другой промокод — освобождаем его использование.
            await db.release_promo(reserved_code, message.from_user.id)
        await db.set_order_promo(int(order_id), new_amount, discount, code)
    await _apply_window(
        bot, message.chat.id, window_id,
        f"✅ Промокод <code>{esc(code)}</code> применён!\n"
        f"Скидка: <b>{price(discount, cfg.currency)}</b>, к оплате: <b>{price(new_amount, cfg.currency)}</b>",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ К оплате", callback_data=f"order:show:{order_id}")
        ]]),
    )
    await notify_admins(bot, cfg,
                        f"🎟 Промокод <code>{esc(code)}</code> применён пользователем "
                        f"<code>{message.from_user.id}</code> (заказ #{order_id})")


# ---------------------------------------------------------------------------
# Оплата
# ---------------------------------------------------------------------------
async def _render_checkout(call: CallbackQuery, cfg: Config, order_id: int, amount: int, url: str) -> None:
    """Показывает окно оплаты с уже существующим счётом (переиспользование ссылки)."""
    text = (
        f"💳 <b>Оплата заказа #{order_id}</b>\n\n"
        f"Сумма: <b>{price(amount, cfg.currency)}</b>\n"
        "Нажмите «Перейти к оплате» — откроется страница ЮKassa (карта, СБП, SberPay).\n\n"
        "После оплаты вернитесь в бот и нажмите «🔄 Проверить оплату». "
        "Если включены вебхуки — доступ выдастся автоматически."
    )
    await _edit_or_answer(call, text, checkout_kb(url, order_id) if url
                          else back_kb(f"order:show:{order_id}"))


async def _show_granted_subscription(call: CallbackQuery, db: Database, cfg: Config, *,
                                     header: str = "✅ <b>Оплата получена — подписка готова!</b>") -> None:
    """Показывает карточку выданной подписки в текущем окне.

    ``complete_order`` уже отправил карточку через ``ui_text``, но следующая
    строка затирала её сухим текстом «ключ отправлен отдельным сообщением» —
    и пользователь оставался без ссылки, QR и кнопок (карточка в режиме одного
    окна и есть то самое «отдельное сообщение»). Поэтому после выдачи окно
    перерисовываем именно карточкой. Если подписки почему-то нет — остаётся
    прежний текстовый результат с кнопкой возврата.
    """
    sub = await db.active_subscription(call.from_user.id)
    if sub is None:
        await _edit_or_answer(call, header, back_kb())
        return
    await send_subscription_card(call.bot, cfg, db, call.from_user.id, sub, header=header)


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, db: Database, cfg: Config, bot: Bot, yk=None) -> None:
    parts = call.data.split(":")
    action, order_id = parts[1], int(parts[2])
    order = await db.get_order(order_id)
    if not _is_owner(order, call.from_user.id):
        await call.answer("Заказ не найден.", show_alert=True)
        return
    # «Одно окно»: если это старый незакрытый заказ, а у пользователя уже есть
    # более новый — платим именно по новому (иначе можно оплатить два заказа).
    if order["status"] == "pending":
        current = await db.user_pending_order(call.from_user.id, order["kind"])
        if current is not None and int(current["id"]) != order_id:
            await _show_order(call, db, cfg, int(current["id"]))
            await call.answer("⚠️ У вас есть более новый заказ — используйте его.",
                              show_alert=True)
            return
    # Заказ, оплаченный, но с упавшей выдачей (grant_error), — НЕ «неактуален»:
    # он ждёт повтора выдачи, иначе деньги списаны, а периода нет.
    needs_regrant = order["status"] == "paid" and bool(order["grant_error"])
    if order["status"] != "pending" and not needs_regrant:
        # Оплаченный заказ: повторное нажатие «Оплатить» НЕ должно вести в ЮKassa
        # (иначе пользователь платит второй раз за уже выданный доступ).
        if order["status"] == "paid":
            await call.answer("✅ Заказ уже оплачен.", show_alert=True)
        else:
            await call.answer("Заказ уже неактуален.", show_alert=True)
        return
    amount = int(order["amount"])

    # Отвечаем на нажатие СРАЗУ: дальше идут запросы в ЮKassa и выдача в панели
    # (до 15–20 с на каждый), а Telegram отклоняет ответ, отправленный позже ~30 с
    # («query is too old…»). Спиннер гаснет сразу, а все результаты — в едином окне.
    await ui.safe_answer(call)

    if action == "check":
        # Пользователь ждёт: ниже опрос ЮKassa по всем счетам заказа и выдача
        # в панели — показываем статус в окне до тяжёлой работы.
        await _working(call, "⏳ Проверяю оплату…", back_kb(f"order:show:{order_id}", "◀️ К заказу"))
        if needs_regrant:
            try:
                await complete_order(bot, cfg, db, order_id, payment_id=order["payment_id"])
            except PanelError as exc:
                await _edit_or_answer(
                    call,
                    f"⚠️ Выдача не удалась: {esc(exc)}\n\nПовторим автоматически — "
                    "период появится в течение пары минут.",
                    back_kb(f"order:show:{order_id}", "◀️ К заказу"),
                )
                return
            await _show_granted_subscription(call, db, cfg,
                                             header="✅ <b>Подписка выдана!</b>")
            return
        paid_id: str | None = None
        if yk is not None:
            for pid in payment_ids_of(order):
                if await yk.is_paid(pid, amount):
                    paid_id = pid
                    break
        if paid_id:
            try:
                await complete_order(bot, cfg, db, order_id, payment_id=paid_id)
            except PanelError as exc:
                await _edit_or_answer(
                    call,
                    f"⚠️ Оплата прошла, но выдача задержалась: {esc(exc)}\n\n"
                    "Повторим автоматически — заказ не потеряется.",
                    back_kb(f"order:show:{order_id}", "◀️ К заказу"),
                )
                return
            await _show_granted_subscription(call, db, cfg)
        else:
            await _edit_or_answer(
                call,
                "⏳ <b>Платёж пока не подтверждён</b>\n\n"
                "Оплатите по ссылке или подождите пару минут и нажмите «🔄 Проверить оплату» снова.",
                checkout_kb(order["payment_url"], order_id) if order["payment_url"]
                else back_kb(f"order:show:{order_id}", "◀️ К заказу"),
            )
        return

    if action == "yookassa":
        if yk is None:
            await _edit_or_answer(call, "⚠️ Оплата картой временно недоступна, попробуйте позже.",
                                  back_kb(f"order:show:{order_id}", "◀️ К заказу"))
            return
        # Повторное нажатие «Оплатить» по заказу, у которого уже есть счёт(а):
        # сначала переспрашиваем статус в ЮKassa (до 15 с на каждый счёт) —
        # показываем статус в окне. Первое нажатие (счетов нет) идёт сразу
        # к созданию счёта, чтобы ссылка появлялась без лишнего мигания.
        if order["payment_id"] or payment_ids_of(order):
            await _working(call, "⏳ Проверяю оплату…", back_kb(f"order:show:{order_id}", "◀️ К заказу"))
        # 1) Любой из ранее выставленных счетов заказа уже оплачен? (история в
        #    orders.payment_ids) — тогда выдаём доступ, новый счёт не создаём.
        for pid in payment_ids_of(order):
            if pid == (order["payment_id"] or ""):
                continue  # 2) текущий счёт проверим ниже — на «переиспользование»
            try:
                existing = await yk.get_payment(pid)
            except PaymentError as exc:
                log.warning("Заказ #%s: не удалось прочитать счёт %s: %s", order_id, pid, exc)
                await _edit_or_answer(
                    call,
                    "⏳ Не удалось проверить оплату — попробуйте через минуту.",
                    back_kb(f"order:show:{order_id}", "◀️ К заказу"),
                )
                return
            if yk.payment_is_valid(existing, amount):
                try:
                    await complete_order(bot, cfg, db, order_id, payment_id=pid)
                except PanelError as exc:
                    await _edit_or_answer(
                        call,
                        f"⚠️ Оплата прошла, но выдача задержалась: {esc(exc)}\n\n"
                        "Повторим автоматически — заказ не потеряется.",
                        back_kb(f"order:show:{order_id}", "◀️ К заказу"),
                    )
                    return
                await _show_granted_subscription(call, db, cfg)
                return
        # Один заказ — один счёт: если у заказа уже есть привязанный платёж,
        # переспрашиваем его статус в API и переиспользуем ту же ссылку.
        # Новый счёт создаётся только если старый отменён/истёк (или запись о
        # нём повреждена) — иначе повторные нажатия «Перейти к оплате» давали
        # несколько рабочих ссылок на один заказ, а продление проходило лишь
        # по одному проведённому платежу (остальные — списание без выдачи).
        if order["payment_id"]:
            try:
                payment = await yk.get_payment(order["payment_id"])
            except PaymentError as exc:
                log.warning("ЮKassa: статус счёта заказа #%s неизвестен: %s", order_id, exc)
                await _edit_or_answer(
                    call,
                    "⚠️ ЮKassa временно недоступна — попробуйте через минуту.",
                    back_kb(f"order:show:{order_id}", "◀️ К заказу"),
                )
                return
            if yk.payment_is_valid(payment, amount):
                # Платёж уже прошёл, но выдача не состоялась (вебхук потерян) —
                # завершаем заказ этим же платежом.
                try:
                    await complete_order(bot, cfg, db, order_id, payment_id=order["payment_id"])
                except PanelError as exc:
                    await _edit_or_answer(
                        call,
                        f"⚠️ Оплата прошла, но выдача задержалась: {esc(exc)}\n\n"
                        "Повторим автоматически — заказ не потеряется.",
                        back_kb(f"order:show:{order_id}", "◀️ К заказу"),
                    )
                    return
                await _show_granted_subscription(call, db, cfg)
                return
            if yk.payment_is_payable(payment):
                stored_url = order["payment_url"] or ""
                if not stored_url:
                    stored_url = (payment.get("confirmation") or {}).get("confirmation_url", "")
                    if stored_url:  # чиним старые заказы, где ссылка не сохранилась
                        await db.claim_order_invoice(order_id, order["payment_id"], stored_url)
                # Счёт тот же — показываем окно оплаты с прежней ссылкой.
                await _render_checkout(call, cfg, order_id, amount, stored_url)
                return
            # Счёт отменён/истёк — освобождаем слот и выпускаем новый.
            await db.release_order_invoice(order_id)
        try:
            payment = await yk.create_payment(
                amount_rub=amount,
                description=f"Price {order['plan_code']} (заказ #{order_id})",
                metadata={"order_id": order_id, "user_id": call.from_user.id},
                # Ключ уникален на каждую попытку создания счёта: после
                # отмены/истечения счёта ЮKassa отвергает повторное использование
                # того же Idempotence-Key в течение 24 часов (HTTP 400
                # "idempotence key already used"). Гонку параллельных кликов
                # гасит не ключ, а БД: claim_order_invoice + payment_is_payable
                # выше. Если ответ ЮKassa потерялся в сети, счёт просто остаётся
                # «сиротой» в ЮKassa и истекает по ttl — заказ не помечается
                # оплаченным без реального платежа.
                idempotence_key=f"order-{order_id}-{uuid.uuid4().hex}",
                ttl_seconds=max(60, cfg.invoice_ttl_minutes * 60),
            )
        except PaymentError as exc:
            log.error("ЮKassa: %s", exc)
            await _edit_or_answer(
                call,
                "❌ Не удалось создать платёж — попробуйте позже или напишите в поддержку.",
                back_kb(f"order:show:{order_id}", "◀️ К заказу"),
            )
            await notify_admins(bot, cfg, f"⚠️ Ошибка ЮKassa по заказу #{order_id}: {esc(exc)}")
            return
        url = (payment.get("confirmation") or {}).get("confirmation_url", "")
        claimed = await db.claim_order_invoice(order_id, payment["id"], url)
        if not claimed:
            # Гонка: параллельный клик уже привязал свой счёт — переиспользуем его,
            # «лишний» платёж с тем же idempotence-ключом в ЮKassa не создастся.
            fresh = await db.get_order(order_id)
            if fresh is not None and fresh["status"] == "paid":
                try:
                    await complete_order(bot, cfg, db, order_id, payment_id=fresh["payment_id"])
                except PanelError as exc:
                    await _edit_or_answer(
                        call,
                        f"⚠️ Оплата прошла, но выдача задержалась: {esc(exc)}\n\n"
                        "Повторим автоматически — заказ не потеряется.",
                        back_kb(f"order:show:{order_id}", "◀️ К заказу"),
                    )
                    return
                await _show_granted_subscription(call, db, cfg)
                return
            # Гонка: чужой клик уже привязал счёт — показываем ту же ссылку.
            await _render_checkout(call, cfg, order_id, amount,
                                   (fresh["payment_url"] if fresh else "") or url)
            return
        await _render_checkout(call, cfg, order_id, amount, url)
        return


# ---------------------------------------------------------------------------
# Мои ключи
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:keys")
async def cb_keys(call: CallbackQuery, db: Database, cfg: Config) -> None:
    subs = await db.user_subscriptions(call.from_user.id)
    if not subs:
        await _edit_or_answer(
            call,
            "У вас пока нет подписок 🙂",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Купить VPN", callback_data="menu:buy")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main")],
            ]),
        )
        await call.answer()
        return
    if len(subs) == 1:
        # У пользователя ровно одна подписка (обычный случай) — список из одной
        # строки был бы лишним шагом, открываем карточку сразу.
        await _render_sub_card(call, db, cfg, int(subs[0]["id"]))
        await call.answer()
        return
    text = "🔑 <b>Мои подписки</b>\n\nВыберите подписку, чтобы посмотреть конфиг, QR-код и инструкцию."
    await _edit_or_answer(call, text, keys_kb(subs))
    await call.answer()


async def _render_sub_card(call: CallbackQuery, db: Database, cfg: Config, sub_id: int) -> bool:
    """Показывает карточку подписки. False — подписка не найдена или чужая."""
    sub = await db.get_subscription(sub_id)
    if sub is None or int(sub["user_id"]) != call.from_user.id:
        await call.answer("Подписка не найдена.", show_alert=True)
        return False
    link = pick_link(sub)
    text = (
        f"🔑 <b>Подписка #{sub_id}</b>\n\n"
        f"Тариф: <b>{esc(sub['plan_code'])}</b>\n"
        f"Статус: {STATUS_RU.get(sub['status'], sub['status'])}\n"
        f"Действует до: <b>{fmt_dt(sub['expires_at'])}</b> (осталось {human_left(sub['expires_at'])})\n"
        f"Устройств: {sub['devices']}, трафик: "
        f"{'безлимит' if not sub['traffic_gb'] else str(sub['traffic_gb']) + ' ГБ'}\n"
        f"🔗 <code>{esc(link)}</code>"

    )
    markup = subscription_kb(sub_id, sub["sub_url"], sub["vpn_key"] or "")
    await _edit_or_answer(call, text, markup)
    return True


@router.callback_query(F.data.startswith("sub:view:"))
async def cb_sub_view(call: CallbackQuery, db: Database, cfg: Config) -> None:
    await _render_sub_card(call, db, cfg, int(call.data.rsplit(":", 1)[1]))
    await call.answer()


async def _load_own_sub(call: CallbackQuery, db: Database) -> dict | None:
    """Возвращает строку подписки по callback ``sub:<action>:<id>``.

    Если подписка не найдена или принадлежит другому пользователю — отвечает
    alert'ом и возвращает None.
    """
    sub_id = int(call.data.rsplit(":", 1)[1])
    sub = await db.get_subscription(sub_id)
    if sub is None or int(sub["user_id"]) != call.from_user.id:
        await call.answer("Подписка не найдена.", show_alert=True)
        return None
    return sub


def _sub_back_kb(sub_id: int) -> InlineKeyboardMarkup:
    """Клавиатура возврата для подэкранов подписки (как у «Как подключиться»)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад к подписке", callback_data=f"sub:view:{sub_id}")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("sub:copy:"))
async def cb_sub_copy(call: CallbackQuery, db: Database) -> None:
    sub = await _load_own_sub(call, db)
    if sub is None:
        return
    link = pick_link(sub)
    if not link:
        await call.answer("Ссылка ещё не готова.", show_alert=True)
        return
    # Логика как у «Как подключиться»: редактируем текущее сообщение (одно окно),
    # добавляем возврат «◀️ Назад к подписке». Ссылка — моноширинным блоком, копируется
    # тапом по ней (Telegram копирует <code>-текст нажатием).
    text = (
        f"🔗 <b>Ссылка подписки #{sub['id']}</b>\n\n"
        f"<code>{esc(link)}</code>\n\n"
        "Нажмите на ссылку, чтобы скопировать, либо удержите сообщение и выберите «Копировать»."
    )
    await _edit_or_answer(call, text, _sub_back_kb(int(sub["id"])))
    await call.answer()


@router.callback_query(F.data.startswith("sub:qr:"))
async def cb_sub_qr(call: CallbackQuery, db: Database, bot: Bot) -> None:
    from aiogram.types import BufferedInputFile

    sub = await _load_own_sub(call, db)
    if sub is None:
        return
    link = sub["sub_url"] or sub["vpn_key"] or ""
    png = make_qr_png(link)
    if png is None:
        await call.answer("Генерация QR недоступна (не установлен модуль qrcode).", show_alert=True)
        return
    # QR — это фото, текст в него не правится: показываем его в том же окне
    # (прошлое сообщение удаляем) с кнопкой возврата к подписке.
    await ui.ui_photo(
        bot, call.message.chat.id, BufferedInputFile(png, filename="vpn-qr.png"),
        caption=f"📷 <b>QR-код подписки #{sub['id']}</b>\nОтсканируйте его в приложении VPN.",
        reply_markup=_sub_back_kb(int(sub["id"])),
    )
    await call.answer()


@router.callback_query(F.data.startswith("sub:howto:"))
async def cb_sub_howto(call: CallbackQuery, db: Database) -> None:
    sub = await _load_own_sub(call, db)
    if sub is None:
        return
    await _edit_or_answer(call, apps_instructions(sub["vpn_key"] or "", sub["sub_url"], int(sub["devices"])),
                          back_kb(f"sub:view:{sub['id']}", "◀️ Назад к подписке"))
    await call.answer()


@router.callback_query(F.data.startswith("sub:renew:"))
async def cb_sub_renew(call: CallbackQuery, db: Database, cfg: Config) -> None:
    sub = await _load_own_sub(call, db)
    if sub is None:
        return
    # Логика как у «Как подключиться»: редактируем текущее сообщение и даём
    # возврат «◀️ Назад к подписке» (тарифы открываются поверх карточки подписки).
    user_id = call.from_user.id
    existing = await db.active_subscription(user_id)
    if existing is not None and not renewal_available(existing, cfg):
        window = renewal_window_days(cfg)
        opens_at = from_iso(existing["expires_at"]) - timedelta(days=window)
        text = (
            "⏳ <b>Продление пока недоступно</b>\n\n"
            f"Продлить подписку можно за <b>{window} дн.</b> до её окончания — "
            "мы обязательно напомним.\n\n"
            f"Сейчас осталось: <b>{human_left(existing['expires_at'])}</b> "
            f"(до {fmt_dt(existing['expires_at'])}).\n"
            f"Окно продления откроется <b>{fmt_dt(opens_at)}</b>."
        )
        await _edit_or_answer(call, text, _sub_back_kb(int(sub["id"])))
        await call.answer()
        return
    plans = await db.active_plans()
    if not plans:
        await _edit_or_answer(
            call,
            "😔 Сейчас нет доступных тарифов. Загляните позже или напишите в поддержку.",
            _sub_back_kb(int(sub["id"])),
        )
        await call.answer()
        return
    text = (
        "🔄 <b>Продление подписки</b>\n\n"
        f"Тариф: <b>{esc(sub['plan_code'])}</b>, действует до: <b>{fmt_dt(sub['expires_at'])}</b>.\n"
        f"Оплата: {_methods_line(cfg)}.\n\n"
        "Выберите тариф — конфиг останется тем же, срок продлевается."
    )
    await _edit_or_answer(call, text, plans_kb(plans, cfg.currency))
    await call.answer()


# ---------------------------------------------------------------------------
# Пробный период
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:trial")
async def cb_trial(call: CallbackQuery, db: Database, cfg: Config, bot: Bot) -> None:
    if not rt.trial_enabled:
        await call.answer("Пробный период сейчас недоступен.", show_alert=True)
        return
    active = await db.active_subscription(call.from_user.id)
    if active is not None:
        await _edit_or_answer(call, "У вас уже есть активная подписка 🙂", back_kb("menu:main"))
        await call.answer()
        return
    # Атомарно «занимаем» триал (trial_used 0 → 1) ДО выдачи: два быстрых
    # нажатия подряд не создадут две пробные подписки (защита от гонки).
    if not await db.claim_trial(call.from_user.id):
        await _edit_or_answer(call, "🎁 Пробный период уже был использован — доступен только платный тариф.",
                              back_kb("menu:buy", "🛒 Купить VPN"))
        await call.answer()
        return
    # Отвечаем на нажатие СРАЗУ: выдача ключа в панели занимает десятки секунд,
    # а Telegram отклоняет ответ позже ~30 с («query is too old…»). Результат
    # пользователь видит в том же окне — карточкой подписки.
    await ui.safe_answer(call)
    await _working(call, "⏳ Создаю доступ…", back_kb("menu:main"))
    try:
        await grant_subscription(bot, cfg, db, call.from_user.id, "trial")
    except PanelError as exc:
        # Выдача не удалась — возвращаем право на триал, чтобы можно было повторить.
        await db.set_user_field(call.from_user.id, "trial_used", 0)
        await _edit_or_answer(call, f"⚠️ Не удалось выдать пробный доступ: {esc(exc)}\nНапишите в поддержку.",
                              back_kb("menu:support", "🆘 Поддержка"))
        await notify_admins(bot, cfg, f"⚠️ Пробный доступ не выдан <code>{call.from_user.id}</code>: {esc(exc)}")
        return
    except Exception:
        await db.set_user_field(call.from_user.id, "trial_used", 0)
        raise
    await notify_admins(bot, cfg, f"🎁 Пробный период выдан <code>{call.from_user.id}</code>")


# ---------------------------------------------------------------------------
# Профиль
# ---------------------------------------------------------------------------
async def _profile_text(db: Database, cfg: Config, user_id: int) -> str:
    sub = await db.active_subscription(user_id)
    orders = await db.user_orders(user_id, limit=100)
    paid = sum(1 for o in orders if o["status"] == "paid")
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"🛒 Покупок: <b>{paid}</b>\n"
    )
    if sub is not None:
        text += (
            f"\n🟢 Подписка: <b>{esc(sub['plan_code'])}</b>\n"
            f"⏳ Осталось: <b>{human_left(sub['expires_at'])}</b> (до {fmt_dt(sub['expires_at'])})\n"
        )
    else:
        text += "\n🔴 Активной подписки нет.\n"
    return text


@router.callback_query(F.data == "menu:profile")
async def cb_profile(call: CallbackQuery, db: Database, cfg: Config) -> None:
    text = await _profile_text(db, cfg, call.from_user.id)
    await _edit_or_answer(call, text, profile_kb())
    await call.answer()


@router.callback_query(F.data == "orders:history")
async def cb_orders_history(call: CallbackQuery, db: Database, cfg: Config) -> None:
    orders = await db.user_orders(call.from_user.id, limit=15)
    if not orders:
        await call.answer("Заказов пока нет.", show_alert=True)
        return
    lines = ["📜 <b>Последние заказы</b>\n"]
    for order in orders:
        lines.append(
            f"#{order['id']} · {esc(order['plan_code'])} · {price(int(order['amount']), cfg.currency)} · "
            f"{STATUS_RU.get(order['status'], order['status'])} · {order['created_at'][:10]}"
        )
    await _edit_or_answer(call, "\n".join(lines), back_kb("menu:profile", "◀️ В профиль"))
    await call.answer()


# ---------------------------------------------------------------------------
# Поддержка
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:support")
async def cb_support(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext) -> None:
    await state.clear()  # эта кнопка — цель «Отмены» шагов ввода
    ticket = await db.open_ticket(call.from_user.id)
    text = (
        "🆘 <b>Поддержка</b>\n\n"
        "Опишите проблему — ответим в этом чате. Обычно отвечаем в течение пары часов.\n"
        "Полезно сразу приложить: номер заказа, скриншот ошибки, название приложения."
    )
    if ticket is not None:
        text += f"\n\n💬 У вас есть открытое обращение #{ticket['id']}."
    await _edit_or_answer(call, text, support_kb(ticket is not None))
    await call.answer()


@router.callback_query(F.data == "support:faq")
async def cb_faq(call: CallbackQuery) -> None:
    await _edit_or_answer(call, FAQ_TEXT, back_kb("menu:support", "◀️ В поддержку"))
    await call.answer()


@router.callback_query(F.data == "support:new")
async def cb_support_new(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    ticket = await db.open_ticket(call.from_user.id)
    if ticket is not None:
        await state.update_data(ticket_id=ticket["id"])
        await state.set_state(TicketStates.reply)
        await _prompt_window(call, state, f"💬 Продолжаем обращение #{ticket['id']}. Напишите сообщение:")
        await call.answer()
        return
    await state.set_state(TicketStates.subject)
    await _prompt_window(call, state, "✍️ Кратко опишите тему обращения (например: «Не работает на iPhone»):",
                         cancel_kb("menu:support"))
    await call.answer()


@router.message(TicketStates.subject)
async def msg_ticket_subject(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    window_id = (await state.get_data()).get(UI_WINDOW_KEY)
    subject = (message.text or "").strip()[:100]
    if not subject:
        await _apply_window(bot, message.chat.id, window_id, "Тема не может быть пустой, попробуйте снова.")
        return
    ticket_id = await db.create_ticket(message.from_user.id, subject)
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(TicketStates.message)
    await _apply_window(bot, message.chat.id, window_id,
                        "📝 Теперь опишите проблему подробнее (можно приложить ссылку или скриншот):")
    await notify_admins(
        bot, cfg,
        f"🧾 <b>Новое обращение #{ticket_id}</b>\nПользователь: <code>{message.from_user.id}</code>\n"
        f"Тема: {esc(subject)}\n\nОтветить: раздел «🧾 Тикеты» в админке.",
    )


@router.message(TicketStates.message)
async def msg_ticket_message(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id") or 0)
    window_id = data.get(UI_WINDOW_KEY)
    await state.clear()
    if not ticket_id:
        await _apply_window(bot, message.chat.id, window_id,
                            "Обращение не найдено, начните заново.", back_kb("menu:support"))
        return
    text = message.text or message.caption or "[вложение]"
    await db.add_ticket_message(ticket_id, False, text)
    await _apply_window(bot, message.chat.id, window_id,
                        f"✅ Сообщение отправлено в поддержку (обращение #{ticket_id}). Ответ придёт сюда же.",
                        main_menu(cfg.is_admin(message.from_user.id), rt.trial_enabled))
    await notify_admins(
        bot, cfg,
        f"💬 <b>Обращение #{ticket_id}</b> — новое сообщение от <code>{message.from_user.id}</code>:\n{esc(text)}",
    )


@router.message(TicketStates.reply)
async def msg_ticket_reply(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id") or 0)
    window_id = data.get(UI_WINDOW_KEY)
    await state.clear()
    text = message.text or message.caption or "[вложение]"
    await db.add_ticket_message(ticket_id, False, text)
    await _apply_window(bot, message.chat.id, window_id, "✅ Отправлено. Ответ придёт в этот чат.",
                        main_menu(cfg.is_admin(message.from_user.id), rt.trial_enabled))
    await notify_admins(bot, cfg, f"💬 <b>Обращение #{ticket_id}</b>: сообщение от <code>{message.from_user.id}</code>\n{esc(text)}")


# ---------------------------------------------------------------------------
# Помощь и правовые документы
# ---------------------------------------------------------------------------
@router.message(Command("help"))
async def msg_help(message: Message, cfg: Config, bot: Bot) -> None:
    await ui.ui_text(bot, message.chat.id, FAQ_TEXT, back_kb("menu:main"))


@router.callback_query(F.data == "menu:help")
async def cb_help(call: CallbackQuery) -> None:
    await _edit_or_answer(call, FAQ_TEXT, back_kb("menu:main"))
    await call.answer()


@router.message(Command("terms"))
async def cmd_terms(message: Message, cfg: Config, bot: Bot) -> None:
    text = cfg.terms_text or (
        "📄 <b>Условия использования</b>\n\n"
        "1. Сервис предоставляется «как есть» для личного использования.\n"
        "2. Запрещено использовать сервис для незаконных действий, спама, атак и рассылок.\n"
        "3. При нарушении правил доступ отключается без возврата средств.\n"
        "4. Один конфиг — до указанного в тарифе количества устройств.\n"
        "5. Возврат средств — по решению поддержки в течение 3 дней с момента оплаты."
    )
    await ui.ui_text(bot, message.chat.id, text, back_kb("menu:main"))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, cfg: Config, bot: Bot) -> None:
    text = cfg.privacy_text or (
        "🔒 <b>Политика конфиденциальности</b>\n\n"
        "• Мы храним только Telegram ID, username и данные о заказах — для выдачи доступа и поддержки.\n"
        "• Данные не передаются третьим лицам, кроме платёжного провайдера (для проведения оплаты).\n"
        "• Трафик пользователя не логируется.\n"
        "• Удалить свои данные можно, обратившись в поддержку."
    )
    await ui.ui_text(bot, message.chat.id, text, back_kb("menu:main"))


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin_denied(call: CallbackQuery) -> None:
    """Сюда попадают админ-кнопки, нажатые обычным пользователем (в т.ч. из старого сообщения)."""
    await call.answer("⛔️ Недостаточно прав.", show_alert=True)
