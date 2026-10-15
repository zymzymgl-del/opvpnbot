"""Typed contracts shared by the support engine and its adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class ToolName(StrEnum):
    FIND_USER_ORDERS = "find_user_orders"
    GET_PAYMENT_STATUS = "get_payment_status"
    GET_SUBSCRIPTION_STATUS = "get_subscription_status"
    CHECK_VPN_KEY = "check_vpn_key"
    TEST_VPN_CONNECTION = "test_vpn_connection"
    RETRY_SUBSCRIPTION_PROVISIONING = "retry_subscription_provisioning"
    CREATE_SUPPORT_TICKET = "create_support_ticket"


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identity and request metadata from the trusted transport adapter."""

    telegram_id: int
    request_id: str
    locale: str = "ru"

    def __post_init__(self) -> None:
        if self.telegram_id <= 0:
            raise ValueError("telegram_id must be a positive Telegram user ID")
        if not self.request_id or len(self.request_id) > 128:
            raise ValueError("request_id must contain 1..128 characters")


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Sanitized result returned by a trusted backend adapter."""

    ok: bool
    code: str
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)
    needs_human: bool = False


@dataclass(frozen=True, slots=True)
class ModelTurn:
    """One model response; the core supports at most one tool call per turn."""

    text: str = ""
    tool_call: ToolCall | None = None


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    role: str
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_arguments: Mapping[str, Any] | None = None
    tool_result: ToolResult | None = None


@dataclass(frozen=True, slots=True)
class EngineReply:
    text: str
    handoff: bool = False
    handoff_reason: str = ""
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: ToolName
    description: str
    parameters: Mapping[str, str]
    required: frozenset[str] = frozenset()
    enabled: bool = True

    def public_schema(self) -> dict[str, Any]:
        """Return a constrained, provider-neutral function schema."""
        return {
            "name": self.name.value,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: {"type": kind} for key, kind in self.parameters.items()
                },
                "required": sorted(self.required),
                "additionalProperties": False,
            },
        }
