"""Mask obvious credentials and payment-card numbers before storage or AI calls."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TELEGRAM_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b")
_VPN_URI = re.compile(r"\b(?:vless|vmess|trojan|ss|ssr)://\S+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b")
_LABELED_SECRET = re.compile(
    r"(?i)\b(?:[A-Za-z0-9_-]*?(?:api[_-]?key|secret(?:[_-]?key)?|password|passwd|token|"
    r"authorization|private[_-]?key|client[_-]?secret))\b"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def _is_luhn_number(raw: str) -> bool:
    digits = [int(ch) for ch in raw if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


@dataclass(frozen=True, slots=True)
class RedactedText:
    text: str
    changed: bool


def redact_sensitive(text: str) -> RedactedText:
    original = text
    text = _TELEGRAM_TOKEN.sub("[токен скрыт]", text)
    text = _VPN_URI.sub("[VPN-ссылка скрыта]", text)
    text = _JWT.sub("[токен скрыт]", text)
    text = _LABELED_SECRET.sub(
        lambda match: match.group(0).split(match.group(1), 1)[0]
        + match.group(1) + "[секрет скрыт]",
        text,
    )
    text = _BEARER_TOKEN.sub("Bearer [токен скрыт]", text)
    text = _CARD_CANDIDATE.sub(
        lambda match: "[данные карты скрыты]" if _is_luhn_number(match.group()) else match.group(),
        text,
    )
    return RedactedText(text=text, changed=text != original)
