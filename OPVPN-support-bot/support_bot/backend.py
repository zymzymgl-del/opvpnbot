"""Support-side tool adapter for tickets and explicitly enabled account reads."""

from __future__ import annotations

from support_bot.core.types import RequestContext, ToolName, ToolResult
from support_bot.opvpn_readonly import OPVPNReadOnlyStore
from support_bot.panel_status_client import OPVPNPanelStatusClient
from support_bot.providers.yookassa_readonly import (
    YooKassaCheckError,
    YooKassaReadOnlyClient,
)
from support_bot.storage import SupportStore


class SupportBackend:
    def __init__(
        self,
        store: SupportStore,
        opvpn_store: OPVPNReadOnlyStore | None = None,
        panel_status_client: OPVPNPanelStatusClient | None = None,
        yookassa_client: YooKassaReadOnlyClient | None = None,
    ) -> None:
        self._store = store
        self._opvpn_store = opvpn_store
        self._panel_status_client = panel_status_client
        self._yookassa_client = yookassa_client

    async def invoke(
        self,
        *,
        context: RequestContext,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolResult:
        if tool_name == ToolName.FIND_USER_ORDERS.value and self._opvpn_store is not None:
            rows = await self._opvpn_store.find_user_orders(
                context.telegram_id,
                int(arguments.get("limit", 5)),
            )
            return ToolResult(
                ok=True,
                code="orders_found" if rows else "no_orders",
                message="Recent orders for the authenticated user.",
                data={"orders": rows},
            )
        if tool_name == ToolName.GET_PAYMENT_STATUS.value and self._opvpn_store is not None:
            row = await self._opvpn_store.get_order_payment_check_data(
                context.telegram_id,
                int(arguments["order_id"]),
            )
            if row is None:
                return ToolResult(
                    ok=True,
                    code="order_not_found",
                    message="No matching order was found for this Telegram account.",
                )
            provisioning_issue = bool(row.get("provisioning_issue"))
            payment_ids = row.pop("payment_ids_internal", ())
            stored_status = str(row.get("status", "unknown"))
            row.pop("provisioning_issue", None)
            if self._yookassa_client is None or not payment_ids:
                safe_row = {
                    "amount": row.get("amount"),
                    "method": row.get("method"),
                    "status": stored_status,
                    "created_at": row.get("created_at"),
                    "paid_at": row.get("paid_at"),
                }
                return ToolResult(
                    ok=True,
                    code="payment_status_found",
                    message="Stored payment state from the OPVPN order record; no live provider lookup was made.",
                    data={"payment": safe_row, "provisioning_issue": provisioning_issue},
                )

            provider_results: list[dict[str, object]] = []
            failed_checks = 0
            for payment_id in payment_ids:
                try:
                    provider_results.append(await self._yookassa_client.get_payment_status(
                        str(payment_id),
                        int(row["amount"]),
                    ))
                except YooKassaCheckError:
                    failed_checks += 1
            succeeded = [
                item for item in provider_results
                if item["status"] == "succeeded" and item["paid"] is True
            ]
            confirmed = any(item["matches_order_amount"] is True for item in succeeded)
            if confirmed:
                provider_status = "paid"
            elif failed_checks:
                provider_status = "unknown"
            elif succeeded:
                provider_status = "amount_mismatch"
            elif any(item["status"] in {"pending", "waiting_for_capture"} for item in provider_results):
                provider_status = "pending"
            else:
                provider_status = "not_paid"
            return ToolResult(
                ok=True,
                code="provider_payment_checked" if provider_status != "unknown" else "provider_check_unavailable",
                message="Sanitized YooKassa status; payment references and credentials are withheld.",
                data={
                    "stored_status": stored_status,
                    "provider_status": provider_status,
                    "provider_checked": bool(provider_results),
                    "checked_payment_count": len(provider_results),
                    "provider_error_count": failed_checks,
                    "provisioning_issue": provisioning_issue,
                    "payments": provider_results,
                },
                needs_human=(provider_status in {"unknown", "amount_mismatch"} or provisioning_issue),
            )
        if tool_name == ToolName.GET_SUBSCRIPTION_STATUS.value and self._opvpn_store is not None:
            rows = await self._opvpn_store.get_subscription_status(context.telegram_id)
            return ToolResult(
                ok=True,
                code="subscriptions_found" if rows else "no_subscriptions",
                message="Stored subscription state for the authenticated Telegram account.",
                data={"subscriptions": rows},
            )
        if tool_name == ToolName.CHECK_VPN_KEY.value and self._opvpn_store is not None:
            subscription_id = int(arguments["subscription_id"])
            record = await self._opvpn_store.check_key_record(
                context.telegram_id,
                subscription_id,
            )
            if record is None:
                return ToolResult(
                    ok=True,
                    code="subscription_not_found",
                    message="No matching subscription was found for this Telegram account.",
                )
            panel_status = None
            if self._panel_status_client is not None:
                panel_status = await self._panel_status_client.check_subscription(
                    context.telegram_id,
                    subscription_id,
                )
            requires_human = not bool(record["key_record_present"])
            if panel_status is not None and (
                not panel_status["panel_reachable"]
                or panel_status["status"] in {"not_found", "unknown"}
            ):
                requires_human = True
            return ToolResult(
                ok=True,
                code="key_record_checked",
                message=(
                    "Local subscription and key-record state checked. Panel verification "
                    "was not performed unless panel_status is present."
                ),
                data={
                    "subscription_record": record,
                    "panel_status": panel_status,
                    "panel_check_performed": panel_status is not None,
                },
                needs_human=requires_human,
            )
        if tool_name == ToolName.CREATE_SUPPORT_TICKET.value:
            category = str(arguments["category"]).strip()[:80]
            summary = str(arguments["summary"]).strip()[:1500]
            ticket_id = await self._store.create_ai_handoff(
                context.telegram_id, category or "other", summary
            )
            return ToolResult(
                ok=True,
                code="ticket_created",
                message="The case was saved for a human specialist.",
                data={"ticket_id": ticket_id},
                needs_human=True,
            )
        # Unknown and not-yet-integrated tools fail closed. In particular, the AI
        # cannot perform payment or subscription changes through this adapter.
        return ToolResult(
            ok=False,
            code="integration_not_available",
            message="This account check is not connected yet.",
            needs_human=True,
        )
