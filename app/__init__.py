"""OPVPN — Telegram-бот для продажи VPN-подписок на aiogram 3.

Исходники: https://github.com/zymzymgl-del/opvpnbot
Основа — DF VPN Bot (автор @Dolov07KBR, https://github.com/Dolov07KBR); см. LICENSE.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

#: Базовая semver-версия продукта. Бампается вручную на релизе (`git tag v2.1.0`):
#: в рантайме к ней приклеивается конкретная сборка (см. ``__build__``).
__version__ = "2.0.0"

#: Автор базового проекта (DF VPN Bot); кредит сохранён по требованию лицензии MIT.
__author__ = "Dolov07KBR"
__author_url__ = "https://github.com/Dolov07KBR"
__repo__ = "https://github.com/zymzymgl-del/opvpnbot"

#: Корень репозитория (пакет app/ лежит в нём) — нужен для версии из git.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_describe() -> str:
    """``git describe --tags --always --dirty`` рабочей копии — версия из git.

    Пусто, если это не git-репозиторий (в Docker-образе и в установке ``install.sh``
    каталога ``.git`` нет) или git недоступен. Никогда не падает: версия —
    вспомогательная информация, её отсутствие не должно мешать запуску.
    """
    if not (_REPO_ROOT / ".git").exists():
        return ""
    try:
        res = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _resolve_build() -> str:
    """Версия сборки: env ``APP_VERSION`` → git → ``__version__``.

    ``APP_VERSION`` задают окружение и ``install.sh``/Docker — это единственный
    надёжный путь там, где нет ``.git`` (копия файлов, образ). Переменная читается
    на импорте модуля, поэтому её надо пробросить ДО запуска процесса
    (в systemd это ``EnvironmentFile``, в Docker — ``ENV``).
    """
    explicit = os.getenv("APP_VERSION", "").strip()
    if explicit:
        return explicit
    described = _git_describe()
    if not described:
        return __version__
    cleaned = described.lstrip("v")
    # "2.1.0", "2.0.0-8-g1a2b3c4" — это уже версия; "1a2b3c4" (тегов нет) — хеш.
    if re.match(r"^\d+\.\d+", cleaned):
        return cleaned
    return f"{__version__}+{cleaned}"


#: Полная версия сборки для показа: «2.0.0», «2.0.0-8-g1a2b3c4» или «2.0.0+1a2b3c4».
__build__ = _resolve_build()

