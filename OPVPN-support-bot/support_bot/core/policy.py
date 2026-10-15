"""Server-owned tool allowlist, argument checks, and risk policy."""

from __future__ import annotations

from typing import Any, Mapping

from support_bot.core.types import ToolCall, ToolName, ToolSpec


# Retry is deliberately disabled until the OPVPN adapter proves ownership,
# current payment status, grant_error eligibility, and cross-process idempotency.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        ToolName.FIND_USER_ORDERS,
        "Find recent orders belonging to the authenticated user.",
        {"limit": "integer"},
    ),
    ToolSpec(
        ToolName.GET_PAYMENT_STATUS,
        "Check the current payment status for one of the authenticated user's orders.",
        {"order_id": "integer"},
        frozenset({"order_id"}),
    ),
    ToolSpec(
        ToolName.GET_SUBSCRIPTION_STATUS,
        "Get stored subscription statuses for the authenticated user without returning VPN keys or URLs.",
        {},
    ),
    ToolSpec(
        ToolName.CHECK_VPN_KEY,
        "Check panel state for one subscription owned by the authenticated user; never return the key.",
        {"subscription_id": "integer"},
        frozenset({"subscription_id"}),
    ),
    ToolSpec(
        ToolName.TEST_VPN_CONNECTION,
        "Run an approved connectivity diagnostic for a subscription owned by the authenticated user.",
        {"subscription_id": "integer"},
        frozenset({"subscription_id"}),
    ),
    ToolSpec(
        ToolName.RETRY_SUBSCRIPTION_PROVISIONING,
        "Retry provisioning only after all payment and idempotency safeguards are implemented.",
        {"order_id": "integer"},
        frozenset({"order_id"}),
        enabled=False,
    ),
    ToolSpec(
        ToolName.CREATE_SUPPORT_TICKET,
        "Escalate the case to a human with a short category and a concise summary. Do not invent diagnostics.",
        {"category": "string", "summary": "string"},
        frozenset({"category", "summary"}),
    ),
)

_SPECS: dict[str, ToolSpec] = {spec.name.value: spec for spec in TOOL_SPECS}


class ToolPolicyError(ValueError):
    """Raised when a model requests a tool or argument outside server policy."""


def enabled_tools(allowed: frozenset[ToolName] | None = None) -> tuple[ToolSpec, ...]:
    return tuple(
        spec for spec in TOOL_SPECS
        if spec.enabled and (allowed is None or spec.name in allowed)
    )


def validate_tool_call(
    call: ToolCall,
    allowed: frozenset[ToolName] | None = None,
) -> tuple[ToolName, dict[str, Any]]:
    spec = _SPECS.get(call.name)
    if spec is None or not spec.enabled or (allowed is not None and spec.name not in allowed):
        raise ToolPolicyError("Tool is unknown or disabled")
    if not isinstance(call.arguments, Mapping):
        raise ToolPolicyError("Tool arguments must be an object")

    args = dict(call.arguments)
    allowed = set(spec.parameters)
    if set(args) - allowed:
        raise ToolPolicyError("Tool arguments contain unsupported fields")
    missing = spec.required - set(args)
    if missing:
        raise ToolPolicyError("Required tool arguments are missing")

    for key, value in args.items():
        kind = spec.parameters[key]
        if kind == "integer":
            # bool is an int subclass, but is never a valid identifier.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolPolicyError(f"{key} must be an integer")
            if value <= 0 and key.endswith("_id"):
                raise ToolPolicyError(f"{key} must be positive")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > 2000:
                raise ToolPolicyError(f"{key} must be a string of at most 2000 characters")
            args[key] = value.strip()

    if "limit" in args and not 1 <= args["limit"] <= 10:
        raise ToolPolicyError("limit must be between 1 and 10")
    return spec.name, args
