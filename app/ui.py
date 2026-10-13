"""Реестр «текущего окна» бота: единое сообщение на чат.

Идея «одного окна»: у пользователя есть одно сообщение-«окно», которое
перерисовывается при переходах, вместо «лестницы» новых сообщений.

Модуль хранит id последнего окна на каждый чат (в памяти процесса) и даёт
хелперы:

* :func:`ui_text` — показать текст в окне (правка, а если нельзя — удалить
  прошлое окно и отправить новое);
* :func:`ui_photo` — показать фото в окне (текст править в фото нельзя, поэтому
  прошлое окно удаляем и шлём фото новым сообщением).

Окна помнятся только в рамках процесса: после рестарта бота первое сообщение
просто отправится заново — это безопасно.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot

log = logging.getLogger(__name__)

#: chat_id -> message_id последнего «окна» в этом чате.
_windows: dict[int, int] = {}


def remember(chat_id: int, message_id: int) -> None:
    """Запоминает сообщение как текущее окно чата."""
    _windows[int(chat_id)] = int(message_id)


def get_window(chat_id: int) -> int | None:
    return _windows.get(int(chat_id))


def forget(chat_id: int) -> None:
    _windows.pop(int(chat_id), None)


async def _delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:  # сообщение уже удалено/старое — не критично
        pass


async def ui_text(bot: Bot, chat_id: int, text: str, reply_markup: Any = None) -> None:
    """Показывает текст в едином окне чата.

    Пытаемся отредактировать прошлое окно; если нельзя (окно это фото, оно
    устарело/удалено или текст не изменился) — удаляем прошлое окно и шлём
    новое, запоминая его id.
    """
    chat_id = int(chat_id)
    window = _windows.get(chat_id)
    if window is not None:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=window,
                reply_markup=reply_markup, disable_web_page_preview=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            if "not modified" in str(exc).lower():
                return  # текст тот же — окно уже актуально
            await _delete(bot, chat_id, window)
    try:
        msg = await bot.send_message(
            chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True,
        )
        if getattr(msg, "message_id", None):
            remember(chat_id, msg.message_id)
    except Exception as exc:  # noqa: BLE001 — пользователь мог заблокировать бота
        log.info("Не удалось отправить окно в чат %s: %s", chat_id, exc)


async def ui_photo(bot: Bot, chat_id: int, photo: Any, caption: str = "",
                   reply_markup: Any = None) -> None:
    """Показывает фото в едином окне (прошлое окно удаляем: текст в фото не правится)."""
    chat_id = int(chat_id)
    window = _windows.pop(chat_id, None)
    if window is not None:
        await _delete(bot, chat_id, window)
    try:
        msg = await bot.send_photo(chat_id, photo, caption=caption, reply_markup=reply_markup)
        if getattr(msg, "message_id", None):
            remember(chat_id, msg.message_id)
    except Exception as exc:  # noqa: BLE001
        log.info("Не удалось отправить фото в чат %s: %s", chat_id, exc)
