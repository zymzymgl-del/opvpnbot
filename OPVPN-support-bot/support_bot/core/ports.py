"""Ports implemented by provider, persistence, and OPVPN adapters."""

from __future__ import annotations

from typing import Protocol, Sequence

from support_bot.core.types import (
    ConversationMessage,
    ModelTurn,
    RequestContext,
    ToolResult,
    ToolSpec,
)


class SupportModel(Protocol):
    async def respond(
        self,
        *,
        context: RequestContext,
        messages: Sequence[ConversationMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelTurn: ...


class SupportBackend(Protocol):
    async def invoke(
        self,
        *,
        context: RequestContext,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolResult: ...


class ConversationStore(Protocol):
    async def load(self, telegram_id: int) -> Sequence[ConversationMessage]: ...

    async def append(
        self,
        telegram_id: int,
        message: ConversationMessage,
    ) -> None: ...

    async def clear(self, telegram_id: int) -> None: ...
