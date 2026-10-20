"""Выдача VPN-ключей через панель 3x-ui.

* ``xui`` — панель 3x-ui: клиент создаётся/продлевается через API, ссылка собирается сама.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import ssl
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from app.config import Config
from app.db import Database, from_iso, to_iso, utcnow
from app.utils import now_plus

log = logging.getLogger(__name__)


class PanelError(RuntimeError):
    """Ошибка взаимодействия с панелью 3x-ui."""


@dataclass
class IssuedKey:
    vpn_key: str
    sub_url: Optional[str]
    panel: str
    panel_username: str


@dataclass
class ClaimedClient:
    """Существующий клиент панели, найденный для привязки к Telegram."""
    email: str
    sub_id: str
    vpn_key: str
    sub_url: Optional[str]
    expires_at: Optional[datetime]
    devices: int
    traffic_gb: int
    enabled: bool


class BasePanel(ABC):
    name = "base"
    #: Поддерживает ли панель привязку готовых клиентов по subId.
    supports_claim = False

    @abstractmethod
    async def create_client(self, *, user_id: int, days: int, devices: int, traffic_gb: int,
                            plan_code: str) -> IssuedKey: ...

    async def extend_client(self, *, panel_username: str, days: int,
                            devices: int | None = None, traffic_gb: int | None = None) -> bool:
        """Продлить подписку у существующего клиента. True — если продление применено в панели."""
        return False

    async def set_enabled(self, *, panel_username: str, enabled: bool) -> bool:
        return False

    async def restart_xray(self) -> bool:
        """Перезапустить ядро Xray в панели (по умолчанию не поддерживается)."""
        return False

    async def delete_client(self, *, panel_username: str) -> bool:
        return False

    async def list_claimable(self) -> list["ClaimedClient"]:
        """Клиенты панели, доступные для привязки (по умолчанию — ни один)."""
        return []

    async def health(self) -> tuple[bool, str]:
        return True, "проверка не поддерживается"

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 3x-ui
# ---------------------------------------------------------------------------
class XuiPanel(BasePanel):
    name = "xui"
    supports_claim = True

    def __init__(self, cfg: Config) -> None:
        self.base = cfg.xui_url.rstrip("/")
        self.username = cfg.xui_username
        self.password = cfg.xui_password
        self.token = cfg.xui_api_token
        self.inbound_ids = tuple(cfg.xui_inbound_ids) or (
            (cfg.xui_inbound_id,) if cfg.xui_inbound_id else ()
        )
        self.inbound_id = cfg.xui_inbound_id
        self.public_host = cfg.xui_public_host
        self.sub_base = cfg.xui_sub_base
        self.verify_ssl = bool(cfg.xui_verify_ssl)
        self.ca_file = cfg.xui_ca_file or None
        # ssl=False — проверка отключена явно (XUI_VERIFY_SSL=false); иначе SSLContext,
        # проверяющий сертификат панели (системные CA или свой XUI_CA_FILE).
        self.ssl_context: ssl.SSLContext | bool = self._make_ssl_context()
        self._session: Optional[aiohttp.ClientSession] = None
        self._logged_in = False
        # None — схема ещё не определена; True — /panel/api/clients/*, False — классические /inbounds/addClient.
        self._clients_api: bool | None = None

    # --- инфраструктура -------------------------------------------------
    def _make_ssl_context(self) -> ssl.SSLContext | bool:
        """Параметр ``ssl=`` для aiohttp: контекст проверки или False.

        По умолчанию проверяем сертификат панели (системные доверенные CA).
        Для self-signed панели укажите ``XUI_CA_FILE``; полностью отключить
        проверку (небезопасно, MITM) можно только явным ``XUI_VERIFY_SSL=false``.
        """
        if not self.verify_ssl:
            return False
        return ssl.create_default_context(cafile=self.ca_file)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def _login(self, session: aiohttp.ClientSession) -> None:
        if self.token:
            self._logged_in = True
            return
        async with session.post(
            f"{self.base}/login",
            data={"username": self.username, "password": self.password},
            ssl=self.ssl_context,
        ) as resp:
            text = await resp.text()
        if resp.status >= 400 or '"success":true' not in text.replace(" ", ""):
            raise PanelError(f"Не удалось авторизоваться в 3x-ui (HTTP {resp.status})")
        self._logged_in = True

    async def _api(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        session = await self._get_session()
        if not self._logged_in:
            await self._login(session)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.base}/panel/api{path}"
        kwargs: dict[str, Any] = {"headers": headers, "ssl": self.ssl_context}
        if payload is not None:
            kwargs["json"] = payload
        async with session.request(method, url, **kwargs) as resp:
            raw = await resp.text()
            if resp.status in (401, 403):
                self._logged_in = False
                raise PanelError("3x-ui: сессия истекла или нет прав (проверьте логин/пароль/токен).")
            if resp.status >= 400:
                raise PanelError(f"3x-ui вернул HTTP {resp.status}: {raw[:200]}")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise PanelError(f"3x-ui вернул не JSON: {raw[:200]}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise PanelError(f"3x-ui: {data.get('msg') or 'неизвестная ошибка'}")
        return data if isinstance(data, dict) else {}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # --- работа с inbounds ----------------------------------------------
    async def _list_inbounds(self) -> list[dict]:
        data = await self._api("/inbounds/list")
        return list(data.get("obj") or [])

    async def _inbounds(self) -> list[dict]:
        """Inbound'ы, в которые бот добавляет клиента (все из XUI_INBOUND_IDS).

        Клиент с одним email/subId попадает сразу во все эти inbound'ы, поэтому
        подписка /sub/{subId} отдаёт конфиги со всеми шифрованиями/транспортами.
        Пустой список = взять первый inbound панели (старое поведение).
        """
        inbounds = await self._list_inbounds()
        if not inbounds:
            raise PanelError("3x-ui: в панели нет ни одного inbound.")
        if not self.inbound_ids:
            return [inbounds[0]]
        by_id = {int(inbound.get("id") or 0): inbound for inbound in inbounds}
        selected: list[dict] = []
        for inbound_id in self.inbound_ids:
            inbound = by_id.get(inbound_id)
            if inbound is None:
                raise PanelError(f"3x-ui: inbound id={inbound_id} не найден.")
            selected.append(inbound)
        return selected

    async def _inbound(self) -> dict:
        """Первый из выбранных inbound'ов (для одиночных операций/совместимости)."""
        return (await self._inbounds())[0]

    async def _has_configured_reality(self) -> bool:
        """Есть ли среди настроенных XUI_INBOUND_IDS inbound с reality.

        Используется для классического API (один inbound на выдачу): в
        мульти-inbound режиме всегда заведомо присутствует инбаунд с
        reality+tls, и flow для vless ставится безусловно.
        """
        try:
            return _has_reality_inbound(await self._inbounds())
        except PanelError:
            return False

    @staticmethod
    def _clients(inbound: dict) -> list[dict]:
        return list(_as_dict(inbound.get("settings")).get("clients") or [])

    async def _find_client(self, email: str) -> tuple[dict, dict] | None:
        inbound = await self._inbound()
        for client in self._clients(inbound):
            if client.get("email") == email:
                return inbound, client
        return None

    # --- схема API: классическая или «clients» ---------------------------
    async def _uses_clients_api(self) -> bool:
        """Определить схему API панели (кэшируется).

        Новые сборки 3x-ui вынесли управление клиентами в /panel/api/clients/*
        и убрали /inbounds/addClient|updateClient|delClient. Проверяем наличием
        /clients/list: 404 → классическая схема.
        """
        if self._clients_api is None:
            try:
                await self._api("/clients/list")
                self._clients_api = True
            except PanelError as exc:
                # Маршрут отсутствует именно в классической схеме → 404.
                # Прочие ошибки (сеть/авторизация) не кэшируем, чтобы не уйти
                # ошибочно в классическую ветку.
                if "HTTP 404" not in str(exc):
                    raise
                self._clients_api = False
        return self._clients_api

    async def _get_client(self, email: str) -> dict:
        data = await self._api(f"/clients/get/{quote(email, safe='')}")
        return (data.get("obj") or {}).get("client") or {}

    @staticmethod
    def _client_identity(client: dict, protocol: str) -> str:
        if protocol == "trojan":
            return str(client.get("password") or client.get("uuid") or "")
        return str(client.get("uuid") or client.get("password") or client.get("auth") or "")

    @staticmethod
    def _update_payload(client: dict) -> dict:
        """Полное тело для /clients/update — панель заменяет объект целиком.

        Во входной структуре ``id`` — это идентификатор клиента (для vless/vmess
        UUID, для trojan пароль), а не числовой id записи; ``allowedIPs``
        принимается массивом. Остальные поля переносим «как есть» из ответа get,
        иначе update обнулит их.
        """
        payload = dict(client)
        payload["id"] = XuiPanel._client_identity(client, "vless")
        # limitHwid панель может вернуть в объекте клиента, но обновление
        # применяет его заново. Держим значение 0 = без ограничений: на 3x-ui 3.x
        # этот лимит гейтит подписку, и клиенты без заголовка X-HWID получают 404.
        payload["limitHwid"] = 0
        allowed = payload.get("allowedIPs")
        if isinstance(allowed, str):
            payload["allowedIPs"] = [v.strip() for v in allowed.split(",") if v.strip()]
        return payload

    async def _create_client_clients_api(self, inbounds: list[dict], *, user_id: int,
                                         days: int, devices: int, traffic_gb: int,
                                         plan_code: str) -> IssuedKey:
        first = inbounds[0]
        protocol = str(first.get("protocol") or "vless")
        settings = self._clients(first)
        template = settings[0] if settings else {}
        email = f"tg{user_id}_{plan_code}_{int(time.time())}"
        expiry_ms = int(now_plus(days).timestamp() * 1000)
        # flow (xtls-rprx-vision): в мульти-inbound режиме (XUI_INBOUND_IDS)
        # всегда есть как минимум один inbound с reality+tls, поэтому если
        # среди выбранных инбаундов встречается reality — flow для vless
        # ставим клиенту безусловно, не требуя применимости ко всем инбаундам.
        # В шаблоне может лежать пустой flow — тогда берём дефолт для vless.
        multi_reality = len(inbounds) > 1 and _has_reality_inbound(inbounds)
        flow_ok = protocol == "vless" and (
            multi_reality or _flow_applies_to_all(inbounds)
        )
        flow = ""
        if flow_ok:
            flow = template.get("flow") or "xtls-rprx-vision"
        client: dict[str, Any] = {
            "email": email,
            "enable": True,
            "expiryTime": expiry_ms,
            "totalGB": traffic_gb * 1024 ** 3,
            "limitIp": max(1, devices),
            # limitHwid НЕ ставим: в 3x-ui 3.x это не «лимит устройств», а гейт
            # подписочного сервиса — при limitHwid > 0 запрос подписки без
            # заголовка X-HWID (v2rayN, v2rayNG, NekoBox и т.п.) получает 404, и
            # ссылка «не резолвится», хотя в браузере открывается HTML-страница.
            # Лимит устройств обеспечивает limitIp.
            "tgId": int(user_id),
            # Один subId на все inbound'ы: панель собирает /sub/{subId} по всем
            # инбаундам с этим subId, поэтому разные шифрования попадут в одну подписку.
            "subId": secrets.token_hex(8),
        }
        if flow:
            client["flow"] = flow
        await self._api(
            "/clients/add",
            method="POST",
            payload={"client": client, "inboundIds": [int(i["id"]) for i in inbounds]},
        )
        try:
            created = await self._get_client(email)
        except PanelError:
            # Клиент уже создан на панели, но прочитать его не удалось —
            # откатываем, чтобы не оставлять «мёртвую» запись.
            await self.delete_client(panel_username=email)
            raise
        sub_id = created.get("subId") or client["subId"]
        sub_url = f"{self.sub_base}/{sub_id}" if (self.sub_base and sub_id) else None
        if sub_url:
            # Клиент создан сразу во всех inbound'ах, а подписку /sub/{subId} целиком
            # собирает панель (по всем инбаундам клиента). Бот отдаёт ТОЛЬКО её —
            # прямые ссылки не конструируются.
            return IssuedKey(vpn_key=sub_url, sub_url=sub_url, panel=self.name,
                             panel_username=email)
        # Подписка в панели не включена (нет XUI_SUB_BASE) — собираем прямые ссылки.
        host = self.public_host or _host_from_url(self.base)
        links = [
            _build_link(
                inbound,
                {
                    "id": self._client_identity(created, str(inbound.get("protocol") or "vless")),
                    "flow": created.get("flow", ""),
                    "email": email,
                },
                host,
            )
            for inbound in inbounds
        ]
        return IssuedKey(
            vpn_key="\n".join(link for link in links if link),
            sub_url=None,
            panel=self.name,
            panel_username=email,
        )

    async def _extend_client_clients_api(self, email: str, days: int, devices: int | None = None,
                                         traffic_gb: int | None = None) -> bool:
        try:
            client = await self._get_client(email)
        except PanelError:
            return False
        if not client:
            return False
        now_ms = int(utcnow().timestamp() * 1000)
        current = int(client.get("expiryTime") or 0)
        base = max(current, now_ms) if current else now_ms
        payload = self._update_payload(client)
        payload["expiryTime"] = base + days * 86400 * 1000 if days > 0 else 0
        payload["enable"] = True
        if devices is not None:
            payload["limitIp"] = max(1, devices)
        # Трафик (totalGB) обновляем только если вызывающий явно его передал —
        # иначе смена тарифа не меняла лимит в панели (см. extend_client).
        if traffic_gb is not None:
            payload["totalGB"] = max(0, traffic_gb) * 1024 ** 3
        await self._api(
            f"/clients/update/{quote(email, safe='')}",
            method="POST",
            payload=payload,
        )
        return True

    # --- публичный API ----------------------------------------------------
    async def create_client(self, *, user_id: int, days: int, devices: int, traffic_gb: int,
                            plan_code: str) -> IssuedKey:
        inbounds = await self._inbounds()
        if await self._uses_clients_api():
            return await self._create_client_clients_api(
                inbounds, user_id=user_id, days=days,
                devices=devices, traffic_gb=traffic_gb, plan_code=plan_code,
            )
        if len(inbounds) > 1:
            raise PanelError(
                "3x-ui: несколько inbound'ов (XUI_INBOUND_IDS) поддерживаются только на "
                "актуальных сборках панели (API /panel/api/clients). Обновите 3x-ui "
                "или укажите один XUI_INBOUND_ID."
            )
        return await self._create_client_classic(
            inbounds[0], user_id=user_id, days=days,
            devices=devices, traffic_gb=traffic_gb, plan_code=plan_code,
        )

    async def _create_client_classic(self, inbound: dict, *, user_id: int, days: int,
                                     devices: int, traffic_gb: int, plan_code: str) -> IssuedKey:
        protocol = str(inbound.get("protocol") or "vless")
        settings = self._clients(inbound)
        template = settings[0] if settings else {}
        email = f"tg{user_id}_{plan_code}_{int(time.time())}"
        client_uuid = str(uuid.uuid4())
        sub_id = secrets.token_hex(8)
        expiry_ms = int(now_plus(days).timestamp() * 1000)
        # flow ставим, когда он применим к inbound'у (vless + tcp + tls/reality)
        # либо когда включён мульти-inbound режим (XUI_INBOUND_IDS) и среди
        # настроенных инбаундов заведомо есть inbound с reality+tls — там flow
        # для vless ставим безусловно; в шаблоне может лежать пустой flow —
        # тогда берём дефолт для vless.
        multi_reality = len(self.inbound_ids) > 1 and await self._has_configured_reality()
        flow_ok = protocol == "vless" and (
            multi_reality or _flow_applies_to_all([inbound])
        )
        flow = ""
        if flow_ok:
            flow = template.get("flow") or "xtls-rprx-vision"
        client = {
            "id": client_uuid if protocol != "trojan" else secrets.token_urlsafe(16),
            "flow": flow,
            "email": email,
            "limitIp": max(1, devices),
            # limitHwid не передаём — см. пояснение в _create_client_clients_api.
            "totalGB": traffic_gb * 1024**3,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": str(user_id),
            "subId": sub_id,
            "reset": 0,
        }
        inner = {"clients": [client]}
        if protocol == "vmess":
            inner["clients"][0]["alterId"] = 0
            inner["clients"][0].pop("flow", None)
        await self._api(
            "/inbounds/addClient",
            method="POST",
            payload={"id": inbound["id"], "settings": json.dumps(inner)},
        )
        link = _build_link(inbound, client, self.public_host or _host_from_url(self.base))
        sub_url = f"{self.sub_base}/{sub_id}" if self.sub_base else None
        # Если подписка панели включена — отдаём только её, без прямого конфига.
        return IssuedKey(vpn_key=sub_url or link, sub_url=sub_url, panel=self.name,
                         panel_username=email)

    async def extend_client(self, *, panel_username: str, days: int,
                            devices: int | None = None, traffic_gb: int | None = None) -> bool:
        if await self._uses_clients_api():
            return await self._extend_client_clients_api(panel_username, days, devices=devices,
                                                          traffic_gb=traffic_gb)
        found = await self._find_client(panel_username)
        if not found:
            return False
        inbound, client = found
        expiry_ms = int(now_plus(days).timestamp() * 1000) if days > 0 else 0
        client["expiryTime"] = expiry_ms
        client["enable"] = True
        if devices is not None:
            client["limitIp"] = max(1, devices)
            client.pop("limitHwid", None)
        # totalGB = 0 в 3x-ui означает «безлимит», поэтому traffic_gb=0
        # тоже применяем (переход на безлимитный тариф), а None — не трогаем.
        if traffic_gb is not None:
            client["totalGB"] = max(0, traffic_gb) * 1024 ** 3
        await self._api(
            f"/inbounds/updateClient/{client['id']}",
            method="POST",
            payload={"id": inbound["id"], "settings": json.dumps({"clients": [client]})},
        )
        return True

    async def set_enabled(self, *, panel_username: str, enabled: bool) -> bool:
        if await self._uses_clients_api():
            client = await self._get_client(panel_username)
            if not client:
                return False
            payload = self._update_payload(client)
            payload["enable"] = bool(enabled)
            await self._api(
                f"/clients/update/{quote(panel_username, safe='')}",
                method="POST",
                payload=payload,
            )
            return True
        found = await self._find_client(panel_username)
        if not found:
            return False
        inbound, client = found
        client["enable"] = bool(enabled)
        await self._api(
            f"/inbounds/updateClient/{client['id']}",
            method="POST",
            payload={"id": inbound["id"], "settings": json.dumps({"clients": [client]})},
        )
        return True

    async def restart_xray(self) -> bool:
        """Перезапуск ядра Xray через API панели (``POST /panel/api/server/restartXrayService``).

        ВНИМАНИЕ: это принудительный рестарт — активные соединения клиентов на
        несколько секунд оборвутся и переподключатся сами.
        """
        await self._api("/server/restartXrayService", method="POST")
        return True

    async def delete_client(self, *, panel_username: str) -> bool:
        if await self._uses_clients_api():
            await self._api(
                f"/clients/del/{quote(panel_username, safe='')}",
                method="POST",
            )
            return True
        found = await self._find_client(panel_username)
        if not found:
            return False
        inbound, client = found
        await self._api(f"/inbounds/delClient/{inbound['id']}/{client['id']}", method="POST")
        return True

    async def health(self) -> tuple[bool, str]:
        try:
            inbounds = await self._inbounds()
        except PanelError as exc:
            return False, str(exc)
        parts = [
            f"#{inbound['id']} ({inbound.get('protocol')}:{inbound.get('port')}), "
            f"клиентов: {len(self._clients(inbound))}"
            for inbound in inbounds
        ]
        return True, "inbound'ы: " + "; ".join(parts)

    async def list_claimable(self) -> list[ClaimedClient]:
        """Все клиенты инбаундов, у которых есть subId — кандидаты на привязку."""
        try:
            inbounds = await self._list_inbounds()
        except PanelError:
            return []
        host = self.public_host or _host_from_url(self.base)
        result: list[ClaimedClient] = []
        seen: set[str] = set()
        for inbound in inbounds:
            protocol = str(inbound.get("protocol") or "vless")
            for client in self._clients(inbound):
                sub_id = client.get("subId")
                email = client.get("email")
                if not sub_id or not email:
                    continue
                # Один клиент может присутствовать в нескольких inbound'ах (мульти-
                # inbound) — привязываем его один раз.
                if str(email) in seen:
                    continue
                seen.add(str(email))
                try:
                    link = _build_link(
                        inbound,
                        {"id": self._client_identity(client, protocol),
                         "flow": client.get("flow", ""), "email": email},
                        host,
                    )
                except PanelError:
                    link = ""
                sub_url = f"{self.sub_base}/{sub_id}" if self.sub_base else None
                if not link and not sub_url:
                    # Собрать ссылку не удалось — привязывать нечего.
                    continue
                expiry_ms = int(client.get("expiryTime") or 0)
                total_gb = int(client.get("totalGB") or 0)
                result.append(ClaimedClient(
                    email=str(email),
                    sub_id=str(sub_id),
                    vpn_key=link,
                    sub_url=sub_url,
                    expires_at=datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc) if expiry_ms > 0 else None,
                    devices=max(1, int(client.get("limitIp") or 1)),
                    traffic_gb=total_gb // (1024 ** 3) if total_gb > 0 else 0,
                    enabled=bool(client.get("enable", True)),
                ))
        return result


def _as_dict(value: object) -> dict:
    """streamSettings/settings панель отдаёт то вложенным объектом, то JSON-строкой."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _has_reality_inbound(inbounds: list[dict]) -> bool:
    """True, если хотя бы один inbound использует reality (streamSettings.security)."""
    for inbound in inbounds:
        stream = _as_dict(inbound.get("streamSettings"))
        if stream.get("security") == "reality":
            return True
    return False


def _flow_applies_to_all(inbounds: list[dict]) -> bool:
    """flow (xtls-rprx-vision) корректен для всех inbound'ов: только vless поверх tcp+tls/reality.

    В мульти-inbound режиме (XUI_INBOUND_IDS) всегда присутствует inbound с
    reality+tls, поэтому там flow ставится при наличии хотя бы одного reality
    инбаунда (_has_reality_inbound) и эта строгая проверка не требуется.
    """
    for inbound in inbounds:
        if str(inbound.get("protocol") or "vless") != "vless":
            return False
        stream = _as_dict(inbound.get("streamSettings"))
        if stream.get("network", "tcp") != "tcp" or stream.get("security") not in {"reality", "tls", "xtls"}:
            return False
    return True


def _host_from_url(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0].split(":")[0]
    except IndexError:
        return url


def _build_link(inbound: dict, client: dict, host: str) -> str:
    """Собирает ссылку конфигурации (vless/vmess/trojan) из настроек inbound."""
    protocol = str(inbound.get("protocol") or "vless").lower()
    port = inbound.get("port")
    remark = f"{client.get('email', 'vpn')}"
    stream = _as_dict(inbound.get("streamSettings"))
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    params: dict[str, str] = {"type": network, "security": security}

    if security == "reality":
        reality = stream.get("realitySettings", {})
        params["pbk"] = reality.get("settings", {}).get("publicKey", "")
        params["fp"] = reality.get("settings", {}).get("fingerprint", "chrome")
        names = reality.get("serverNames") or []
        if names:
            params["sni"] = names[0]
        short_ids = reality.get("shortIds") or []
        if short_ids:
            params["sid"] = short_ids[0]
        params["spx"] = "%2F"
    elif security in {"tls", "xtls"}:
        tls = stream.get("tlsSettings", {})
        names = tls.get("serverName") or ""
        if isinstance(names, str) and names:
            params["sni"] = names
        elif isinstance(names, list) and names:
            params["sni"] = names[0]
        alpn = tls.get("alpn") or []
        if alpn:
            params["alpn"] = ",".join(alpn)
        params["fp"] = tls.get("settings", {}).get("fingerprint", "chrome")

    if network == "ws":
        ws = stream.get("wsSettings", {})
        params["path"] = ws.get("path", "/")
        ws_host = (ws.get("headers") or {}).get("Host")
        if ws_host:
            params["host"] = ws_host
    elif network == "grpc":
        grpc = stream.get("grpcSettings", {})
        params["serviceName"] = grpc.get("serviceName", "")
    elif network == "tcp":
        header = (stream.get("tcpSettings") or {}).get("header", {})
        if header.get("type") == "http":
            params["headerType"] = "http"
    elif network in {"httpupgrade", "xhttp"}:
        section = stream.get(f"{network}Settings", {})
        params["path"] = section.get("path", "/")
        if section.get("host"):
            params["host"] = section["host"]

    query = "&".join(f"{k}={quote(str(v), safe='%')}" for k, v in params.items() if v not in (None, ""))

    if protocol == "vless":
        if client.get("flow"):
            query += f"&flow={client['flow']}"
        return f"vless://{client['id']}@{host}:{port}?{query}#{quote(remark)}"

    if protocol == "trojan":
        return f"trojan://{quote(str(client['id']))}@{host}:{port}?{query}#{quote(remark)}"

    if protocol == "vmess":
        payload = {
            "v": "2",
            "ps": remark,
            "add": host,
            "port": str(port),
            "id": client["id"],
            "aid": str(client.get("alterId", 0)),
            "scy": "auto",
            "net": network,
            "type": "none",
            "host": params.get("host", ""),
            "path": params.get("path", ""),
            "tls": "tls" if security in {"tls", "xtls"} else "",
            "sni": params.get("sni", ""),
            "fp": params.get("fp", ""),
        }
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
        return f"vmess://{encoded}"

    raise PanelError(f"3x-ui: протокол {protocol} не поддерживается для генерации ссылки.")


# ---------------------------------------------------------------------------
# Фабрика
# ---------------------------------------------------------------------------
_panel: BasePanel | None = None


def get_panel(cfg: Config, db: Database) -> BasePanel:
    """Синглтон панели 3x-ui (``db`` оставлен для совместимости сигнатуры)."""
    global _panel
    if _panel is None:
        _panel = XuiPanel(cfg)
    return _panel


async def close_panel() -> None:
    global _panel
    if _panel is not None:
        await _panel.close()
        _panel = None


def subscription_payload(sub_row) -> dict:
    """Приводит строку подписки к словарю, нужному панелям."""
    return {
        "panel_username": sub_row["panel_username"],
        "expires_at": from_iso(sub_row["expires_at"]) if isinstance(sub_row["expires_at"], str)
        else sub_row["expires_at"],
    }


__all__ = [
    "BasePanel", "XuiPanel", "PanelError", "IssuedKey", "ClaimedClient",
    "get_panel", "close_panel", "to_iso",
]
