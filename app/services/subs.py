"""Бизнес-логика подписок: выдача, продление, истечение, напоминания."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Config, is_absolute_url
from app.db import Database, from_iso, to_iso, utcnow
from app.health import touch_worker
from app.keyboards import subscription_kb
from app.logctx import set_request_id
from app import ui
from app.services.panels import PanelError, get_panel
from app.runtime import runtime as rt
from app.services.payments import YooKassaClient
from app.utils import esc, fmt_dt, human_left, make_qr_png, price

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------
async def notify_admins(bot: Bot, cfg: Config, text: str, reply_markup=None) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as exc:  # администратор мог заблокировать бота
            log.warning("Не удалось уведомить админа %s: %s", admin_id, exc)


async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("Сообщение пользователю %s не доставлено: %s", chat_id, exc)
        return False
    except Exception as exc:  # pragma: no cover
        log.warning("Ошибка отправки пользователю %s: %s", chat_id, exc)
        return False


# ---------------------------------------------------------------------------
# Выдача и продление подписок
# ---------------------------------------------------------------------------
def renewal_window_days(cfg: Config) -> int:
    """Окно продления = самое раннее напоминание (notify_days_before). 0 — без ограничения."""
    return max(cfg.notify_days_before, default=0)


def renewal_available(existing, cfg: Config) -> bool:
    """Продление доступно только в окне notify_days_before до окончания подписки."""
    if existing is None:
        return True
    window = renewal_window_days(cfg)
    if window <= 0:
        return True
    left = from_iso(existing["expires_at"]) - utcnow()
    return left <= timedelta(days=window)


async def send_subscription_card(bot: Bot, cfg: Config, db: Database, user_id: int, sub_row,
                                 *, with_qr: bool = False,
                                 header: str = "🎉 <b>VPN готов!</b>") -> None:
    """Отправляет пользователю карточку подписки (конфиг + кнопки QR/инструкции).

    ``with_qr=True`` дополнительно отправляет QR-код отдельным сообщением —
    нужно только в сценарии привязки клиента панели (claims), где QR был в ссылке,
    которую пользователь отсканировал. При обычной выдаче/продлении QR не шлём:
    он доступен кнопкой «📷 QR-код».

    ``header`` — заголовок карточки: при перевыдаче сломанного клиента это
    «♻️ Подписка перевыдана», чтобы пользователь понял, что конфиг заменился.
    """
    sub_url = sub_row["sub_url"] if "sub_url" in sub_row.keys() else None
    link = sub_url or (sub_row["vpn_key"] or "")
    text = (
        f"{header}\n\n"
        f"Тариф: <b>{esc(sub_row['plan_code'])}</b>\n"
        f"Действует до: <b>{fmt_dt(sub_row['expires_at'])}</b> "
        f"(осталось {human_left(sub_row['expires_at'])})\n"
        f"Устройств: <b>{sub_row['devices']}</b>, трафик: <b>"
        f"{'безлимит' if not sub_row['traffic_gb'] else str(sub_row['traffic_gb']) + ' ГБ'}</b>\n\n"
    )
    if is_absolute_url(sub_url):
        # Косметика: сама ссылка — моноширинным блоком (<code>), чтобы тап по ней
        # КОПИРОВАЛ адрес, а не открывал сайт: URL внутри <code> Telegram не
        # превращает в ссылку. То же самое делает кнопка «🔗 Скопировать ссылку».
        text += f"🔗 <code>{esc(sub_url)}</code>\n\nНажмите на ссылку, чтобы скопировать."
    else:
        text += "Нажмите кнопку ниже, чтобы получить QR-код и инструкцию по подключению."
    markup = subscription_kb(sub_row["id"], sub_url, sub_row["vpn_key"] or "")
    await ui.ui_text(bot, user_id, text, markup)

    if with_qr and link:
        qr = make_qr_png(link)
        if qr:
            from aiogram.types import BufferedInputFile

            await ui.ui_photo(
                bot, user_id, BufferedInputFile(qr, filename="vpn-qr.png"),
                caption="📷 QR-код вашей конфигурации — отсканируйте в приложении VPN.",
                reply_markup=markup,
            )


async def _apply_reissue(db: Database, sub_id: int, issued, *, shown_plan: str, devices: int,
                        traffic_gb: int, expires_at: datetime) -> None:
    """Перевыдача: обновляет СУЩЕСТВУЮЩУЮ строку подписки данными нового клиента.

    Нужна, когда продлить/возобновить старый клиент панели не получилось
    (он удалён или сломан): бот выпускает нового клиента, но пишет его в ТУ ЖЕ
    строку ``subscriptions``. Так у пользователя не появляется второй подписки.
    """
    await db.update_subscription(
        sub_id,
        vpn_key=issued.vpn_key, sub_url=issued.sub_url,
        panel_username=issued.panel_username,
        expires_at=to_iso(expires_at), status="active", notified="",
        devices=devices, traffic_gb=traffic_gb, plan_code=shown_plan,
    )


async def grant_subscription(bot: Bot, cfg: Config, db: Database, user_id: int, plan_code: str,
                             *, days: Optional[int] = None, devices: Optional[int] = None,
                             traffic_gb: Optional[int] = None, notify: bool = True,
                             quiet_admin: bool = False):
    """Выдаёт доступ: продлевает активную подписку, возобновляет истёкшую тем же
    клиентом панели, а если клиента продлить нельзя — перевыдаёт его в ту же
    строку. Второй подписки у пользователя не появляется никогда."""
    plan = await db.get_plan(plan_code)
    if plan is None and plan_code != "trial":
        raise PanelError(f"Тариф {plan_code} не найден.")
    if plan is not None and not plan["is_active"] and plan_code != "trial":
        raise PanelError("Тариф временно недоступен.")

    if plan_code == "trial":
        days = days or rt.trial_days
        devices = devices or cfg.trial_devices
        traffic_gb = traffic_gb if traffic_gb is not None else cfg.trial_traffic_gb
    else:
        days = days or int(plan["days"])
        devices = devices or int(plan["devices"])
        traffic_gb = traffic_gb if traffic_gb is not None else int(plan["traffic_gb"])

    panel = get_panel(cfg, db)
    active = await db.active_subscription(user_id)

    # Кого продлевать/возобновлять:
    #  * активную подписку — обычное продление;
    #  * если активной нет — последнего истёкшего клиента той же панели, чтобы
    #    вернуть пользователю ТОТ ЖЕ конфиг, а не плодить нового клиента в панели.
    # Второй подписки у пользователя быть не должно (инвариант «одна подписка»).
    reuse = active
    if reuse is None:
        reuse = await db.latest_subscription(user_id, panel.name, status="expired")
    elif reuse["panel"] != panel.name:
        # Активна подписка ДРУГОЙ панели: вторую активную создавать нельзя, а
        # продлить чужого клиента текущей панелью невозможно.
        raise PanelError(
            f"Активна подписка другой панели ({reuse['panel']}) — нужно ручное вмешательство."
        )

    if reuse is not None:
        reactivating = reuse["status"] != "active"
        # Срок считаем от max(прошлое окончание, сейчас): после долгого простоя
        # нельзя «съесть» часть нового периода.
        new_expires = max(from_iso(reuse["expires_at"]), utcnow()) + timedelta(days=days)
        new_devices = devices or 1
        shown_plan = plan_code if plan_code != "trial" else reuse["plan_code"]
        # Устройства задаёт купленный тариф (может и понизить лимит);
        # трафик — не понижаем: счётчик в панели кумулятивный, резкое
        # уменьшение мгновенно отключило бы клиента с уже израсходованным трафиком.
        old_traffic = int(reuse["traffic_gb"] or 0)
        new_traffic = traffic_gb or 0
        merged_traffic = 0 if (old_traffic == 0 or new_traffic == 0) else max(old_traffic, new_traffic)
        extended = await panel.extend_client(
            panel_username=reuse["panel_username"] or "", days=days,
            devices=new_devices, traffic_gb=merged_traffic,
        )
        if extended:
            if reactivating:
                # Клиент был выключен при истечении — включаем обратно (best-effort:
                # некоторые панели уже включают его внутри extend_client).
                try:
                    await panel.set_enabled(panel_username=reuse["panel_username"] or "", enabled=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не удалось включить клиента %s: %s", reuse["panel_username"], exc)
            await db.update_subscription(
                reuse["id"],
                expires_at=to_iso(new_expires),
                status="active",
                notified="",
                devices=new_devices,
                traffic_gb=merged_traffic,
                plan_code=shown_plan,
            )
            refreshed = await db.get_subscription(int(reuse["id"]))
            if notify:
                traffic_label = "безлимит" if not merged_traffic else f"{merged_traffic} ГБ"
                header = "Подписка возобновлена" if reactivating else "Подписка продлена"
                await ui.ui_text(
                    bot, user_id,
                    f"✅ <b>{header} до {fmt_dt(new_expires)}</b> (+{days} дн.)\n"
                    f"Тариф: <b>{esc(shown_plan)}</b> · устройств: <b>{new_devices}</b> · трафик: <b>{traffic_label}</b>\n\n"
                    "Приятного пользования!",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔑 Моя подписка",
                                              callback_data=f"sub:view:{reuse['id']}")],
                        [InlineKeyboardButton(text="🏠 В меню", callback_data="menu:main")],
                    ]),
                )
            if not quiet_admin:
                action = "Возобновление подписки" if reactivating else "Продление подписки"
                await notify_admins(
                    bot, cfg,
                    f"🔄 {action}\nПользователь: <code>{user_id}</code>\n"
                    f"Тариф: {esc(plan_code)}\nСрок: +{days} дн. → до {fmt_dt(new_expires)}",
                )
            return refreshed

        # Продлить/возобновить существующего клиента не удалось — он удалён или
        # сломан в панели. ПЕРЕВЫДАЁМ подписку: выпускаем нового клиента, но
        # пишем его в ТУ ЖЕ строку, чтобы у пользователя не появилось второй подписки.
        log.warning(
            "Клиента %s (%s) продлить не удалось — перевыдаём подписку #%s",
            reuse["panel_username"], reuse["panel"], reuse["id"],
        )
        issued = await panel.create_client(
            user_id=user_id, days=days, devices=new_devices,
            traffic_gb=new_traffic, plan_code=plan_code,
        )
        await _apply_reissue(
            db, int(reuse["id"]), issued, shown_plan=shown_plan,
            devices=new_devices, traffic_gb=new_traffic, expires_at=new_expires,
        )
        refreshed = await db.get_subscription(int(reuse["id"]))
        if notify:
            await send_subscription_card(
                bot, cfg, db, user_id, refreshed,
                header=(
                    "♻️ <b>Подписка перевыдана</b>\n"
                    "Старый конфиг в панели оказался недоступен — мы выпустили новый. "
                    "Переимпортируйте ссылку или QR-код."
                ),
            )
        if not quiet_admin:
            await notify_admins(
                bot, cfg,
                f"♻️ <b>Перевыдача подписки</b>\nПользователь: <code>{user_id}</code>\n"
                f"Тариф: {esc(shown_plan)}\nСрок: +{days} дн. → до {fmt_dt(new_expires)}",
            )
        return refreshed

    # Первая подписка пользователя.
    issued = await panel.create_client(
        user_id=user_id, days=days, devices=devices or 1,
        traffic_gb=traffic_gb or 0, plan_code=plan_code,
    )
    expires_at = utcnow() + timedelta(days=days)
    sub_id = await db.create_subscription(
        user_id=user_id, plan_code=plan_code, vpn_key=issued.vpn_key, sub_url=issued.sub_url,
        panel=issued.panel, panel_username=issued.panel_username, devices=devices or 1,
        traffic_gb=traffic_gb or 0, expires_at=expires_at,
    )
    if not sub_id:
        # Гонка: активная подписка появилась параллельно — перевыдаём в неё,
        # второй строки не создаём.
        existing = await db.active_subscription(user_id)
        if existing is None:
            raise PanelError("Не удалось создать подписку — попробуйте ещё раз.")
        log.warning("Активная подписка появилась параллельно — перевыдаю в #%s", existing["id"])
        await _apply_reissue(
            db, int(existing["id"]), issued, shown_plan=plan_code,
            devices=devices or 1, traffic_gb=traffic_gb or 0, expires_at=expires_at,
        )
        sub_id = int(existing["id"])
    row = await db.get_subscription(int(sub_id))
    if notify:
        # Только карточка подписки: QR — по кнопке, главное меню — по кнопке «📋 Все подписки».
        await send_subscription_card(bot, cfg, db, user_id, row)
    if not quiet_admin:
        await notify_admins(
            bot, cfg,
            f"🆕 Новая подписка\nПользователь: <code>{user_id}</code>\nТариф: {esc(plan_code)}\n"
            f"Срок: {days} дн. (до {fmt_dt(expires_at)})\n",
        )
    return row


# ---------------------------------------------------------------------------
# Ручная перевыдача (админ)
# ---------------------------------------------------------------------------
async def reissue_subscription(bot: Bot, cfg: Config, db: Database, sub_id: int,
                               *, actor_id: int | None = None) -> dict:
    """Ручная перевыдача подписки администратором — когда клиент в панели
    сломан/удалён и старая ссылка не работает.

    Гарантии (главное требование):
      * срок НЕ меняется — сколько дней оставалось, столько и остаётся;
      * тариф, устройства и трафик сохраняются как у подписки (конфиг
        пересоздаётся с теми же параметрами);
      * создаётся НОВЫЙ клиент панели, его ссылкой заменяется ``vpn_key``/
        ``sub_url`` в ТОЙ ЖЕ строке подписки — второй подписки не появляется;
      * старый (сломанный) клиент панели удаляется best-effort.

    Возвращает результат-словарь для админ-обработчика:
      ``{"ok": True,  "row": <обновлённая подписка>, "old_username": ...,
         "new_username": ..., "client_deleted": bool}``
      либо ``{"ok": False, "error": "<текст для админа>"}`` — без изменений.
    """
    sub = await db.get_subscription(sub_id)
    if sub is None:
        return {"ok": False, "error": f"Подписка #{sub_id} не найдена."}

    panel = get_panel(cfg, db)
    if sub["panel"] != panel.name:
        return {
            "ok": False,
            "error": (f"Подписка выпущена панелью «{sub['panel']}», а активна «{panel.name}» — "
                      "перевыдача невозможна (переключите панель или разберитесь вручную)."),
        }

    expires_at = from_iso(sub["expires_at"])
    left_days = max(1, (expires_at - utcnow()).days + 1)  # никогда не выдаём конфиг «в никуда»

    # Сначала выпускаем нового клиента: если панель откажет — старая ссылка
    # останется на месте, подписка не пострадает.
    try:
        issued = await panel.create_client(
            user_id=int(sub["user_id"]), days=left_days,
            devices=int(sub["devices"] or 1), traffic_gb=int(sub["traffic_gb"] or 0),
            plan_code=sub["plan_code"],
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Перевыдача #%s: панель отказала при создании клиента", sub_id)
        return {"ok": False, "error": f"Панель отказала: {exc}. Старая ссылка не тронута."}

    old_username = sub["panel_username"] or ""
    await _apply_reissue(
        db, sub_id, issued, shown_plan=sub["plan_code"],
        devices=int(sub["devices"] or 1), traffic_gb=int(sub["traffic_gb"] or 0),
        expires_at=expires_at,
    )
    refreshed = await db.get_subscription(sub_id)

    client_deleted = False
    if old_username:
        try:
            client_deleted = bool(await panel.delete_client(panel_username=old_username))
        except Exception as exc:  # noqa: BLE001
            log.warning("Перевыдача #%s: удалить старого клиента %s не удалось: %s",
                        sub_id, old_username, exc)

    actor = f" (админ <code>{actor_id}</code>)" if actor_id else ""
    await safe_send(
        bot, int(sub["user_id"]),
        "♻️ <b>Подписка перевыдана вручную</b>\n"
        "Старый конфиг в панели оказался недоступен — мы выпустили новый.\n"
        f"Срок действия НЕ изменился: до {fmt_dt(refreshed['expires_at'])} "
        f"(осталось {human_left(refreshed['expires_at'])}).\n\n"
        "Переимпортируйте новую ссылку или QR-код ниже.",
    )
    await send_subscription_card(
        bot, cfg, db, int(sub["user_id"]), refreshed,
        header="🔑 <b>Новая ссылка вашей подписки</b>",
    )
    await notify_admins(
        bot, cfg,
        f"♻️ <b>Ручная перевыдача подписки #{sub_id}</b>{actor}\n"
        f"Пользователь: <code>{sub['user_id']}</code> · тариф: {esc(sub['plan_code'])}\n"
        f"Срок сохранён: до {fmt_dt(refreshed['expires_at'])} "
        f"(осталось {human_left(refreshed['expires_at'])})\n"
        f"Клиент панели: <code>{esc(old_username)}</code> → <code>{esc(issued.panel_username)}</code>"
        f" ({'старый удалён' if client_deleted else 'старый НЕ удалён — проверьте панель'})",
    )
    log.info("Перевыдача #%s выполнена: пользователь %s, остаток %d дн.",
             sub_id, sub["user_id"], left_days)
    return {
        "ok": True,
        "row": refreshed,
        "old_username": old_username,
        "new_username": issued.panel_username,
        "client_deleted": client_deleted,
    }


# ---------------------------------------------------------------------------
# Оплата заказа
# ---------------------------------------------------------------------------
#: Локи на заказ: одновременно выдавать/повторять выдачу должен ровно один вызов
#: (поллер + кнопка «Проверить оплату» + вебхук работают в одном процессе).
_grant_locks: dict[int, asyncio.Lock] = {}


def _grant_lock(order_id: int) -> asyncio.Lock:
    if len(_grant_locks) > 500:  # подчищаем отпущенные локи, чтобы словарь не рос
        for oid, lock in list(_grant_locks.items()):
            if not lock.locked():
                del _grant_locks[oid]
    return _grant_locks.setdefault(order_id, asyncio.Lock())


def payment_ids_of(order) -> list[str]:
    """Все выставленные счёта заказа.

    Основной источник — ``orders.payment_ids`` (история счетов); запасной —
    ``orders.payment_id`` (на случай строк из старого кода или усечённого
    набора столбцов). Нужно, чтобы оплата по любому счёту заказа находила заказ.
    """
    try:
        raw = order["payment_ids"] or ""
    except (IndexError, KeyError, TypeError):
        raw = ""
    raw = raw or order["payment_id"] or ""
    return [pid for pid in raw.split(",") if pid]


async def complete_order(bot: Bot, cfg: Config, db: Database, order_id: int,
                         *, payment_id: str | None = None, silent: bool = False) -> bool:
    """Помечает заказ оплаченным и выдаёт подписку.

    Идемпотентность обеспечивается атомарным переходом pending -> paid
    (``db.claim_order_for_payment``): при одновременных вебхуке, опросе и
    нажатии «Проверить оплату» заказ будет завершён ровно одним вызовом.

    Ровно как важно и обратное: если заказ уже ``paid``, но в прошлый раз
    ``grant_subscription`` упала (``orders.grant_error``), подписка НЕ выдана и
    заказ нельзя считать завершённым. Такой заказ повторяется автоматически
    (фоновый поллер, «Проверить оплату», админ-кнопка) — иначе деньги уходили
    бы, а период не появлялся бы никогда.
    """
    async with _grant_lock(order_id):
        order = await db.get_order(order_id)
        if order is None:
            log.warning("Заказ #%s не найден", order_id)
            return False

        retry = order["status"] == "paid"
        if retry:
            if not order["grant_error"]:
                log.info("Заказ #%s уже завершён (оплачен и выдан) — повтор пропущен", order_id)
                return True
            log.warning("Заказ #%s оплачен, но выдача не завершена (%s) — повторяем",
                        order_id, order["grant_error"])
        elif not await db.claim_order_for_payment(order_id, payment_id=payment_id):
            order = await db.get_order(order_id)
            if order is None:
                return False
            if order["status"] == "pending":
                log.warning("Не удалось захватить заказ #%s для завершения", order_id)
                return False
            if order["status"] != "paid":
                log.info("Заказ #%s уже завершён (статус: %s) — повтор пропущен",
                         order_id, order["status"])
                return False
            # Заказ только что переведён в paid другим вызовом; он и выдаёт подписку.
            return True

        user_id = int(order["user_id"])

        promo_code = order["promo_code"]
        if promo_code and not int(order["promo_reserved"] or 0):
            # Совместимость: заказы, оформленные до перехода на резервирование,
            # не зафиксировали использование — фиксируем по факту оплаты, если промо
            # ещё валиден. Для новых заказов использование уже зарезервировано в
            # момент применения (msg_promo), повторно списывать нельзя.
            valid, reason, _promo = await db.promo_valid(promo_code, user_id)
            if valid:
                await db.reserve_promo(promo_code, user_id)
            else:
                log.warning(
                    "Заказ #%s оплачен, но промокод %s не зафиксирован (%s) — "
                    "скидка уже применена к сумме",
                    order_id, promo_code, reason,
                )

        if not retry:
            await db.log_payment(user_id, order["amount"], order["method"], "order", f"заказ #{order_id}")

        try:
            await grant_subscription(bot, cfg, db, user_id, order["plan_code"], notify=not silent)
        except Exception as exc:  # noqa: BLE001 — фиксируем сбой и повторяем позже
            await db.set_order_grant_error(order_id, f"{type(exc).__name__}: {exc}")
            await safe_send(
                bot, user_id,
                "⚠️ Оплата получена, но выдать конфиг автоматически не получилось.\n"
                "Мы повторим выдачу автоматически в течение пары минут. "
                "Если доступ не появится — напишите в поддержку.",
            )
            await notify_admins(
                bot, cfg,
                f"🚨 <b>Оплата прошла, но выдача не удалась</b>\nЗаказ #{order_id}, "
                f"пользователь <code>{user_id}</code>\nОшибка: {esc(exc)}\n"
                "Повторю автоматически (фоновая проверка) — или вручную: админка → «Заказы».",
            )
            raise

        await db.set_order_grant_error(order_id, "")

        if retry:
            await notify_admins(
                bot, cfg,
                f"♻️ <b>Повторная выдача по заказу #{order_id} прошла успешно</b>\n"
                f"Пользователь: <code>{user_id}</code>",
            )
        else:
            await notify_admins(
                bot, cfg,
                f"💾 <b>Оплачен заказ #{order_id}</b>\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"Сумма: {price(order['amount'], cfg.currency)}\n"
                f"Способ: {order['method']}\n"
                f"Позиция: {esc(order['plan_code'])} ({order['kind']})",
            )
        return True


# ---------------------------------------------------------------------------
# Фоновые задачи
# ---------------------------------------------------------------------------
async def process_expired(bot: Bot, cfg: Config, db: Database) -> None:
    """Отключает истёкшие подписки в панели и уведомляет пользователей."""
    panel = get_panel(cfg, db)
    for sub in await db.subscriptions_expired():
        if cfg.auto_disable_expired:
            try:
                await panel.set_enabled(panel_username=sub["panel_username"] or "", enabled=False)
            except Exception as exc:
                log.warning("Не удалось отключить клиента %s: %s", sub["panel_username"], exc)
        await db.update_subscription(sub["id"], status="expired")
        await safe_send(
            bot, int(sub["user_id"]),
            "⌛️ <b>Подписка закончилась</b>\n\n"
            f"Тариф: {esc(sub['plan_code'])}\n"
            "Продлите подписку, чтобы вернуть доступ — конфиг останется тем же.",
            None,
        )
        await notify_admins(bot, cfg, f"⌛️ Подписка истекла: <code>{sub['user_id']}</code> ({esc(sub['plan_code'])})")


async def send_reminders(bot: Bot, cfg: Config, db: Database) -> None:
    """Напоминает о скором окончании подписки (за 3 дня / за 1 день по умолчанию)."""
    for sub in await db.subscriptions_expiring():
        left_hours = (from_iso(sub["expires_at"]) - utcnow()).total_seconds() / 3600
        notified = set(filter(None, (sub["notified"] or "").split(",")))
        for days in cfg.notify_days_before:
            marker = f"{days}d"
            if left_hours <= days * 24 and marker not in notified:
                notified.add(marker)
                await db.update_subscription(sub["id"], notified=",".join(sorted(notified)))
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔄 Продлить подписку", callback_data=f"sub:renew:{sub['id']}")
                ]])
                await safe_send(
                    bot, int(sub["user_id"]),
                    f"⏰ <b>Подписка заканчивается</b>\n\nОсталось: <b>{human_left(sub['expires_at'])}</b> "
                    f"(до {fmt_dt(sub['expires_at'])}).\nПродлите вовремя, чтобы не потерять доступ.",
                    markup,
                )
                break


async def poll_yookassa_orders(bot: Bot, cfg: Config, db: Database, client=None) -> None:
    """Резервный путь: если вебхуки недоступны, сами проверяем статус платежей ЮKassa.

    Здесь же повторяются оплаченные заказы, по которым выдача подписки в прошлый
    раз упала (``status='paid'`` + ``grant_error``): их больше не видно среди
    ``pending``, поэтому без отдельного прохода оплата «зависла» бы навсегда.

    ``client`` — переиспользуемый YooKassaClient из bot.py (dp["yk"]); если не
    передан, создаётся локальный экземпляр и закрывается по завершении
    (см. docs/ARCHITECTURE.md, техдолг #3).
    """
    stuck = await db.orders_needing_grant()
    orders = await db.pending_orders(method="yookassa") if cfg.yookassa_enabled else []
    if not orders and not stuck:
        return
    own_client = client is None and bool(orders)
    if own_client:
        client = YooKassaClient(cfg)
    try:
        for order in orders:
            # Счетов у заказа может быть несколько (история в orders.payment_ids):
            # проверяем все, иначе оплата по «старому» счёту потеряется.
            for pid in payment_ids_of(order):
                try:
                    paid = await client.is_paid(pid, int(order["amount"]))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Проверка заказа #%s (%s): %s", order["id"], pid, exc)
                    continue
                if not paid:
                    continue
                try:
                    await complete_order(bot, cfg, db, int(order["id"]), payment_id=pid)
                except Exception as exc:  # noqa: BLE001
                    log.error("Не удалось выдать заказ #%s: %s", order["id"], exc)
                break
        # Повтор выдачи по оплаченным, но не выданным заказам.
        for order in stuck:
            try:
                await complete_order(bot, cfg, db, int(order["id"]), payment_id=order["payment_id"])
            except Exception as exc:  # noqa: BLE001
                log.error("Повтор выдачи заказа #%s не удался: %s", order["id"], exc)
    finally:
        if own_client:
            await client.close()


async def cancel_stale_orders(db: Database, hours: int) -> None:
    for order in await db.stale_orders(hours):
        if order["promo_code"] and int(order["promo_reserved"] or 0):
            await db.release_promo(order["promo_code"], int(order["user_id"]))
            await db.execute("UPDATE orders SET promo_reserved = 0 WHERE id = ?", (int(order["id"]),))
        await db.set_order_status(int(order["id"]), "canceled")


async def run_db_backup(cfg: Config, db: Database) -> str | None:
    """Очередной бэкап БД с ротацией (хранит последние cfg.backup_keep копий).

    Возвращает путь к созданному файлу либо None, если интервал ещё не прошёл.
    """
    if cfg.backup_interval_minutes <= 0:
        return None
    backup_dir = Path(cfg.db_path).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backups = sorted(backup_dir.glob("opvpnbot-*.sqlite3"))
    if backups:
        newest = backups[-1]
        mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
        if utcnow() - mtime < timedelta(minutes=cfg.backup_interval_minutes):
            return None
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"opvpnbot-{stamp}.sqlite3"
    await db.backup(str(dest))
    # Ротация: удаляем старые сверх лимита.
    created = sorted(backup_dir.glob("opvpnbot-*.sqlite3"))
    for old in created[: max(0, len(created) - cfg.backup_keep)]:
        try:
            old.unlink()
        except OSError:
            log.warning("Не удалось удалить старый бэкап %s", old)
    log.info("Бэкап БД создан: %s (%d КБ)", dest.name, dest.stat().st_size // 1024)
    return str(dest)


async def background_worker(bot: Bot, cfg: Config, db: Database, yk=None) -> None:
    """Единый цикл фоновых задач: напоминания, истечение, проверка платежей."""
    set_request_id("worker")  # rid для всех логов воркера (отдельная задача — свой контекст)
    while True:
        # Heartbeat для /healthz: воркер не завис и цикл доходит до новой итерации.
        touch_worker()
        try:
            await process_expired(bot, cfg, db)
            await send_reminders(bot, cfg, db)
            await poll_yookassa_orders(bot, cfg, db, client=yk)
            await cancel_stale_orders(db, cfg.orders_ttl_hours)
            await run_db_backup(cfg, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка в фоновом цикле: %s", exc)
        await asyncio.sleep(180)
