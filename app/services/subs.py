"""Бизнес-логика подписок: выдача, продление, истечение, напоминания, рефералы."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.config import Config
from app.db import Database, from_iso, to_iso, utcnow
from app.keyboards import subscription_kb
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
                                 *, with_qr: bool = False) -> None:
    """Отправляет пользователю карточку подписки (конфиг + кнопки QR/инструкции).

    ``with_qr=True`` дополнительно отправляет QR-код отдельным сообщением —
    нужно только в сценарии привязки клиента панели (claims), где QR был в ссылке,
    которую пользователь отсканировал. При обычной выдаче/продлении QR не шлём:
    он доступен кнопкой «📷 QR-код».
    """
    sub_url = sub_row["sub_url"] if "sub_url" in sub_row.keys() else None
    link = sub_url or (sub_row["vpn_key"] or "")
    text = (
        "🎉 <b>VPN готов!</b>\n\n"
        f"Тариф: <b>{esc(sub_row['plan_code'])}</b>\n"
        f"Действует до: <b>{fmt_dt(sub_row['expires_at'])}</b> "
        f"(осталось {human_left(sub_row['expires_at'])})\n"
        f"Устройств: <b>{sub_row['devices']}</b>, трафик: <b>"
        f"{'безлимит' if not sub_row['traffic_gb'] else str(sub_row['traffic_gb']) + ' ГБ'}</b>\n\n"
        "Нажмите кнопку ниже, чтобы получить QR-код и инструкцию по подключению."
    )
    markup = subscription_kb(sub_row["id"], sub_url, sub_row["vpn_key"] or "", cfg.support_username)
    await safe_send(bot, user_id, text, markup)

    if with_qr and link:
        qr = make_qr_png(link)
        if qr:
            try:
                from aiogram.types import BufferedInputFile

                await bot.send_photo(
                    user_id,
                    BufferedInputFile(qr, filename="vpn-qr.png"),
                    caption="📷 QR-код вашей конфигурации — отсканируйте в приложении VPN.",
                )
            except Exception as exc:  # pragma: no cover
                log.info("QR не отправлен: %s", exc)


async def grant_subscription(bot: Bot, cfg: Config, db: Database, user_id: int, plan_code: str,
                             *, days: Optional[int] = None, devices: Optional[int] = None,
                             traffic_gb: Optional[int] = None, notify: bool = True,
                             quiet_admin: bool = False):
    """Выдаёт доступ: продлевает активную подписку, возобновляет истёкшую тем же
    клиентом панели либо создаёт новую, если переиспользовать клиента нельзя."""
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
    #  * активную подписку той же панели — обычное продление;
    #  * если активной нет — последнего истёкшего клиента той же панели, чтобы
    #    вернуть пользователю ТОТ ЖЕ конфиг, а не плодить нового клиента в панели.
    # Новую подписку создаём, только если активна подписка другой панели либо
    # клиента не удалось продлить (например, он удалён в панели).
    reuse = None
    if active is not None:
        if active["panel"] == panel.name:
            reuse = active
    else:
        reuse = await db.latest_subscription(user_id, panel.name, status="expired")

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
        if extended and reactivating:
            # Клиент был выключен при истечении — включаем обратно (best-effort:
            # некоторые панели уже включают его внутри extend_client).
            try:
                await panel.set_enabled(panel_username=reuse["panel_username"] or "", enabled=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось включить клиента %s: %s", reuse["panel_username"], exc)
        if extended:
            await db.update_subscription(
                reuse["id"],
                expires_at=to_iso(new_expires),
                status="active",
                notified="",
                devices=new_devices,
                traffic_gb=merged_traffic,
                plan_code=shown_plan,
            )
            refreshed = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (reuse["id"],))
            if notify:
                traffic_label = "безлимит" if not merged_traffic else f"{merged_traffic} ГБ"
                header = "Подписка возобновлена" if reactivating else "Подписка продлена"
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                await safe_send(
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
        log.warning(
            "Клиента %s (%s) продлить не удалось — создаём нового",
            reuse["panel_username"], reuse["panel"],
        )

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
    row = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if notify:
        # Только карточка подписки: QR — по кнопке, главное меню — по кнопке «◀️ К подпискам».
        await send_subscription_card(bot, cfg, db, user_id, row)
    if not quiet_admin:
        await notify_admins(
            bot, cfg,
            f"🆕 Новая подписка\nПользователь: <code>{user_id}</code>\nТариф: {esc(plan_code)}\n"
            f"Срок: {days} дн. (до {fmt_dt(expires_at)})\nПанель: {issued.panel}",
        )
    return row


# ---------------------------------------------------------------------------
# Оплата заказа
# ---------------------------------------------------------------------------
async def apply_referral_bonus(bot: Bot, cfg: Config, db: Database, buyer_id: int, amount: int) -> None:
    """Начисляет пригласившему процент от первого/каждого платежа приглашённого."""
    if not cfg.referral_active or rt.ref_percent <= 0:
        return
    user = await db.get_user(buyer_id)
    if user is None or not user["referrer_id"]:
        return
    bonus = amount * rt.ref_percent // 100
    if bonus <= 0:
        return
    referrer_id = int(user["referrer_id"])
    await db.add_balance(referrer_id, bonus, method="referral", kind="referral",
                         note=f"бонус за покупку пользователя {buyer_id}")
    await db.execute(
        "UPDATE users SET ref_earned = ref_earned + ? WHERE telegram_id = ?", (bonus, referrer_id)
    )
    await safe_send(
        bot, referrer_id,
        f"🎉 <b>Реферальный бонус!</b>\nВаш приглашённый совершил покупку, "
        f"на баланс начислено <b>{price(bonus, cfg.currency)}</b>.",
    )


async def complete_order(bot: Bot, cfg: Config, db: Database, order_id: int,
                         *, payment_id: str | None = None, silent: bool = False) -> bool:
    """Помечает заказ оплаченным и выдаёт то, что заказано (подписку или баланс).

    Идемпотентность обеспечивается атомарным переходом pending -> paid
    (``db.claim_order_for_payment``): при одновременных вебхуке, опросе и
    нажатии «Проверить оплату» заказ будет завершён ровно одним вызовом.
    """
    if not await db.claim_order_for_payment(order_id, payment_id=payment_id):
        order = await db.get_order(order_id)
        if order is None:
            log.warning("Заказ #%s не найден", order_id)
            return False
        if order["status"] != "pending":
            log.info("Заказ #%s уже завершён (статус: %s) — повтор пропущен", order_id, order["status"])
            return order["status"] == "paid"
        log.warning("Не удалось захватить заказ #%s для завершения", order_id)
        return False
    order = await db.get_order(order_id)
    if order is None:  # теоретически невозможно, но защищаемся
        return False
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

    if order["kind"] == "topup":
        await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                         (order["amount"], user_id))
        await db.log_payment(user_id, order["amount"], order["method"], "topup", f"заказ #{order_id}")
        if not silent:
            user = await db.get_user(user_id)
            await safe_send(
                bot, user_id,
                f"💰 Баланс пополнен на <b>{price(order['amount'], cfg.currency)}</b>.\n"
                f"Текущий баланс: <b>{price(int(user['balance']) if user else 0, cfg.currency)}</b>",
            )
    else:
        await db.log_payment(user_id, order["amount"], order["method"], "order", f"заказ #{order_id}")
        try:
            await grant_subscription(bot, cfg, db, user_id, order["plan_code"], notify=not silent)
        except PanelError as exc:
            await safe_send(
                bot, user_id,
                "⚠️ Оплата получена, но выдать конфиг автоматически не получилось.\n"
                "Мы уже работаем над этим — администратор выдаст ключ вручную.",
            )
            await notify_admins(
                bot, cfg,
                f"🚨 <b>Оплата прошла, но выдача не удалась</b>\nЗаказ #{order_id}, "
                f"пользователь <code>{user_id}</code>\nОшибка: {esc(exc)}",
            )
            raise
        await apply_referral_bonus(bot, cfg, db, user_id, order["amount"])

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
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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

    ``client`` — переиспользуемый YooKassaClient из bot.py (dp["yk"]); если не
    передан, создаётся локальный экземпляр и закрывается по завершении
    (см. docs/ARCHITECTURE.md, техдолг #3).
    """
    if not cfg.yookassa_enabled:
        return
    orders = await db.pending_orders(method="yookassa")
    if not orders:
        return
    own_client = client is None
    if own_client:
        client = YooKassaClient(cfg)
    try:
        for order in orders:
            if not order["payment_id"]:
                continue
            try:
                paid = await client.is_paid(order["payment_id"], int(order["amount"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("Проверка заказа #%s: %s", order["id"], exc)
                continue
            if paid:
                try:
                    await complete_order(bot, cfg, db, int(order["id"]), payment_id=order["payment_id"])
                except Exception as exc:  # noqa: BLE001
                    log.error("Не удалось выдать заказ #%s: %s", order["id"], exc)
    finally:
        if own_client:
            await client.close()


async def cancel_stale_orders(db: Database, hours: int) -> None:
    for order in await db.stale_orders(hours):
        if order["promo_code"] and int(order["promo_reserved"] or 0):
            await db.release_promo(order["promo_code"], int(order["user_id"]))
            await db.execute("UPDATE orders SET promo_reserved = 0 WHERE id = ?", (int(order["id"]),))
        await db.set_order_status(int(order["id"]), "canceled")


async def poll_yoomoney_orders(bot: Bot, cfg: Config, db: Database, ym) -> None:
    """Автопроверка переводов ЮMoney по label заказа."""
    if ym is None:
        return
    rows = await db.fetchall(
        "SELECT id, amount FROM orders WHERE status = 'pending' AND method = 'yoomoney' "
        "AND kind IN ('subscription', 'topup')"
    )
    for row in rows:
        op_id = await ym.find_payment(row["id"], int(row["amount"]))
        if not op_id:
            continue
        try:
            await complete_order(bot, cfg, db, row["id"], payment_id=op_id)
            log.info("Заказ #%s оплачен через ЮMoney (%s)", row["id"], op_id)
        except PanelError as exc:
            await notify_admins(bot, cfg, f"⚠️ ЮMoney по заказу #{row['id']}: оплата есть, выдача не удалась: {exc}")


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


async def background_worker(bot: Bot, cfg: Config, db: Database, ym=None, yk=None) -> None:
    """Единый цикл фоновых задач: напоминания, истечение, проверка платежей."""
    while True:
        try:
            await process_expired(bot, cfg, db)
            await send_reminders(bot, cfg, db)
            await poll_yookassa_orders(bot, cfg, db, client=yk)
            await poll_yoomoney_orders(bot, cfg, db, ym)
            await cancel_stale_orders(db, cfg.orders_ttl_hours)
            await run_db_backup(cfg, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка в фоновом цикле: %s", exc)
        await asyncio.sleep(180)
