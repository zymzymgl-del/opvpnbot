"""Telegram transport for the support inbox, implemented with aiogram 3."""

from __future__ import annotations

import html
import logging
import secrets
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from support_bot.config import Settings
from support_bot.core.engine import SupportEngine
from support_bot.core.types import ConversationMessage, RequestContext
from support_bot.redaction import redact_sensitive
from support_bot.storage import SupportStore

log = logging.getLogger(__name__)
router = Router(name="support")


def _command_args(message: Message, count: int) -> list[str] | None:
    parts = (message.text or "").split(maxsplit=count)
    if len(parts) <= count:
        return None
    return parts[1:]


async def _notify_admins(bot: Bot, settings: Settings, text: str) -> int:
    sent = 0
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
            sent += 1
        except Exception as exc:  # admins may not have started the support bot yet
            log.info("Could not notify support admin %s: %s", admin_id, type(exc).__name__)
    return sent


async def install_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать обращение"),
        BotCommand(command="help", description="Как связаться с поддержкой"),
        BotCommand(command="delete_me", description="Удалить мои обращения и историю"),
    ])


@router.message(CommandStart())
async def cmd_start(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    if settings.is_admin(message.from_user.id):
        await message.answer(
            "🛠 <b>Бот поддержки готов.</b>\n\n"
            "Команды администратора:\n"
            "/tickets — открытые обращения\n"
            "/reply ID текст — ответить пользователю\n"
            "/close ID — закрыть обращение"
        )
        return
    await message.answer(
        "Здравствуйте! Это техподдержка OPVPN.\n\n"
        "ИИ-диспетчер разберёт сообщение и при необходимости передаст его специалисту. "
        "Текст сообщения может обрабатываться DeepSeek; перед отправкой бот маскирует "
        "некоторые распознаваемые секреты. Опишите проблему и, если есть, номер заказа. "
        "Специалист ответит здесь. Не присылайте пароли, коды подтверждения "
        "или данные банковской карты.\n\n"
        "Локальную историю и обращения можно удалить командой /delete_me."
    )


@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings) -> None:
    if message.from_user and settings.is_admin(message.from_user.id):
        await message.answer("Команды: /tickets, /reply ID текст, /close ID")
    else:
        await message.answer(
            "Напишите сюда описание проблемы. Если вопрос связан с оплатой, "
            "укажите номер заказа и примерное время платежа."
        )


@router.message(Command("delete_me"))
async def cmd_delete_me(message: Message, store: SupportStore, settings: Settings) -> None:
    user = message.from_user
    if user is None or settings.is_admin(user.id):
        return
    await store.erase_user_data(user.id)
    await message.answer(
        "Ваши обращения и локальная история удалены. Эта команда не управляет "
        "данными, которые ранее могли быть переданы ИИ-провайдеру."
    )
    log.info("Support user data erased")


@router.message(Command("tickets"))
async def cmd_tickets(message: Message, store: SupportStore, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.is_admin(user.id):
        return
    tickets = await store.open_tickets(limit=30)
    if not tickets:
        await message.answer("Открытых обращений нет.")
        return
    lines = ["<b>Открытые обращения</b>"]
    for row in tickets:
        lines.append(
            f"#{row['id']} · пользователь <code>{row['telegram_id']}</code> · "
            f"обновлено {html.escape(row['updated_at'])}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("reply"))
async def cmd_reply(message: Message, bot: Bot, store: SupportStore, settings: Settings) -> None:
    admin = message.from_user
    if admin is None or not settings.is_admin(admin.id):
        return
    args = _command_args(message, 2)
    if not args or len(args) < 2:
        await message.answer("Формат: /reply ID текст")
        return
    try:
        ticket_id = int(args[0])
    except ValueError:
        await message.answer("ID обращения должен быть числом.")
        return
    text = args[1].strip()
    if not text or len(text) > 4000:
        await message.answer("Текст ответа должен содержать от 1 до 4000 символов.")
        return
    user_id = await store.add_admin_reply(ticket_id, admin.id, text)
    if user_id is None:
        await message.answer("Открытое обращение с таким ID не найдено.")
        return
    safe_history_text = redact_sensitive(text).text
    await store.append(user_id, ConversationMessage(role="assistant", content=safe_history_text))
    try:
        await bot.send_message(
            user_id,
            f"💬 <b>Ответ поддержки по обращению #{ticket_id}</b>\n\n{html.escape(text)}",
        )
    except Exception as exc:
        log.info("Could not deliver support reply; ticket=%s error=%s", ticket_id, type(exc).__name__)
        await message.answer("Ответ сохранён, но доставить его пользователю не удалось.")
        return
    await message.answer(f"Ответ по обращению #{ticket_id} отправлен.")


@router.message(Command("close"))
async def cmd_close(message: Message, bot: Bot, store: SupportStore, settings: Settings) -> None:
    admin = message.from_user
    if admin is None or not settings.is_admin(admin.id):
        return
    args = _command_args(message, 1)
    if not args:
        await message.answer("Формат: /close ID")
        return
    try:
        ticket_id = int(args[0])
    except ValueError:
        await message.answer("ID обращения должен быть числом.")
        return
    user_id = await store.close_ticket(ticket_id)
    if user_id is None:
        await message.answer("Открытое обращение с таким ID не найдено.")
        return
    try:
        await bot.send_message(user_id, f"✅ Обращение #{ticket_id} закрыто. Если понадобится помощь, напишите снова.")
    except Exception as exc:
        log.info("Could not deliver ticket closure; ticket=%s error=%s", ticket_id, type(exc).__name__)
    await message.answer(f"Обращение #{ticket_id} закрыто.")


@router.message(F.chat.type == "private", F.text)
async def user_text(
    message: Message,
    bot: Bot,
    store: SupportStore,
    settings: Settings,
    engine: SupportEngine,
) -> None:
    user = message.from_user
    if user is None or settings.is_admin(user.id):
        return
    original = (message.text or "").strip()
    if not original or original.startswith("/"):
        return
    redacted = redact_sensitive(original)
    text = redacted.text
    open_ticket_id = await store.open_ticket_for_user(user.id)
    if open_ticket_id is not None:
        await store.append(user.id, ConversationMessage(role="user", content=text))
        ticket_id, _created = await store.add_user_message(user.id, text)
        notified = await notify_ticket_admins(bot, settings, store, ticket_id, user)
        response = f"Сообщение добавлено к открытому обращению #{ticket_id}. Специалист ответит здесь."
        if notified == 0:
            response += " Уведомление специалистам не доставлено."
        await message.answer(response)
        return

    reply = await engine.handle(
        RequestContext(telegram_id=user.id, request_id=secrets.token_hex(12)),
        text,
    )
    if not reply.handoff:
        answer = html.escape(reply.text)
        if redacted.changed:
            answer += "\n\nЯ скрыл распознанные секретные данные перед обработкой."
        await message.answer(answer)
        return

    ticket_id = next((
        int(result.data["ticket_id"])
        for result in reply.tool_results
        if result.code == "ticket_created" and result.data.get("ticket_id")
    ), None)
    if ticket_id is None:
        ticket_id, created = await store.add_user_message(user.id, text)
        if created:
            await store.add_system_note(ticket_id, f"Передача от ИИ: {reply.handoff_reason}")
    notified = await notify_ticket_admins(bot, settings, store, ticket_id, user)
    response = f"Передал обращение #{ticket_id} специалисту. Ответ придёт сюда."
    if redacted.changed:
        response += " Распознанные секретные данные были скрыты."
    if notified == 0:
        response += " Обращение сохранено, но специалисты пока не получили уведомление."
    await message.answer(response)


async def notify_ticket_admins(bot: Bot, settings: Settings, store: SupportStore, ticket_id: int, user) -> int:
    messages = await store.ticket_messages(ticket_id, limit=5)
    username = f"@{html.escape(user.username)}" if user.username else "не указан"
    full_name = html.escape(user.full_name[:150])
    lines = [
        f"🆘 <b>Обращение #{ticket_id}</b>",
        f"Пользователь: {full_name} ({username})",
        f"Telegram ID: <code>{user.id}</code>",
    ]
    labels = {"user": "Пользователь", "admin": "Специалист", "system": "Внутренняя заметка"}
    for item in messages:
        label = labels.get(item["sender"], "Сообщение")
        lines.append(f"\n<b>{label}:</b>\n{html.escape(item['text'][:800])}")
    lines.append(f"\nОтвет: <code>/reply {ticket_id} ваш текст</code>")
    card = "\n".join(lines)
    if len(card) > 3900:
        card = card[:3800] + "\n… переписка обрезана …"
    notified = await _notify_admins(bot, settings, card)
    if notified == 0:
        log.error("No support admins could be notified; ticket=%s", ticket_id)
    return notified


@router.message(F.chat.type == "private", ~F.text)
async def user_attachment(message: Message, settings: Settings) -> None:
    if message.from_user and not settings.is_admin(message.from_user.id):
        await message.answer(
            "Пока я принимаю только текстовые сообщения. Опишите, пожалуйста, "
            "ошибку словами и укажите название приложения и устройства."
        )


def register_handlers(dp: Any) -> None:
    dp.include_router(router)
