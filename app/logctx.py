"""Контекст логирования: короткий ``request_id`` для корреляции записей.

Каждый апдейт Telegram (и каждый вебхук) получает короткий идентификатор,
который автоматически подставляется в лог-сообщения через ``RequestIdFilter``.
Это позволяет по одной строке из ``data/bot.log`` найти все записи, относящиеся
к конкретному нажатию пользователя, платежу или ошибке.

Реализовано на ``contextvars``: значение не «протекает» между задачами
(апдейтами/запросами), поэтому параллельные обработки не путают id.
"""

from __future__ import annotations

import contextvars
import logging
import secrets
from contextlib import contextmanager
from typing import Iterator

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    """Текущий request_id (или ``\"-\"`` вне контекста)."""
    return request_id_var.get()


def set_request_id(value: str) -> contextvars.Token[str]:
    """Устанавливает request_id и возвращает токен для отката."""
    return request_id_var.set(value)


def reset_request_id(token: contextvars.Token[str]) -> None:
    request_id_var.reset(token)


@contextmanager
def log_context(request_id: str | None = None) -> Iterator[str]:
    """Контекст с заданным (или случайным) request_id.

    ``with log_context("worker"):`` — все логи внутри получат rid=worker.
    """
    value = request_id or secrets.token_hex(4)
    token = request_id_var.set(value)
    try:
        yield value
    finally:
        request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    """Подставляет актуальный request_id в каждую лог-запись."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


__all__ = [
    "RequestIdFilter",
    "current_request_id",
    "log_context",
    "request_id_var",
    "reset_request_id",
    "set_request_id",
]
