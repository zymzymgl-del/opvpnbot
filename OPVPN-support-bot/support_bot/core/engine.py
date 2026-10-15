"""Bounded orchestration loop; model output is always treated as untrusted."""

from __future__ import annotations

import asyncio
import logging
import uuid
from weakref import WeakValueDictionary

from support_bot.core.policy import ToolPolicyError, enabled_tools, validate_tool_call
from support_bot.core.ports import ConversationStore, SupportBackend, SupportModel
from support_bot.core.types import (
    ConversationMessage,
    EngineReply,
    RequestContext,
    ToolName,
    ToolResult,
)
from support_bot.redaction import redact_sensitive

log = logging.getLogger(__name__)


class SupportEngine:
    """Run a bounded model/tool loop for one authenticated Telegram user."""

    def __init__(
        self,
        *,
        model: SupportModel,
        backend: SupportBackend,
        store: ConversationStore,
        max_tool_rounds: int = 4,
        model_timeout_seconds: float = 30.0,
        tool_timeout_seconds: float = 12.0,
        max_input_chars: int = 4000,
        allowed_tools: frozenset[ToolName] = frozenset({ToolName.CREATE_SUPPORT_TICKET}),
    ) -> None:
        if not 1 <= max_tool_rounds <= 8:
            raise ValueError("max_tool_rounds must be between 1 and 8")
        if not 1 <= model_timeout_seconds <= 90:
            raise ValueError("model_timeout_seconds must be between 1 and 90")
        if not 0.5 <= tool_timeout_seconds <= 60:
            raise ValueError("tool_timeout_seconds must be between 0.5 and 60")
        if not 1 <= max_input_chars <= 10000:
            raise ValueError("max_input_chars must be between 1 and 10000")
        self._model = model
        self._backend = backend
        self._store = store
        self._max_tool_rounds = max_tool_rounds
        self._model_timeout_seconds = model_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds
        self._max_input_chars = max_input_chars
        self._allowed_tools = allowed_tools
        self._user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    async def handle(self, context: RequestContext, user_text: str) -> EngineReply:
        # Keep the secret filter at the model boundary as well as in Telegram
        # handlers, so alternate callers cannot forward raw secrets.
        text = redact_sensitive(user_text.strip()).text
        if not text:
            return EngineReply("Пожалуйста, напишите вопрос текстом.")
        if len(text) > self._max_input_chars:
            return EngineReply("Сообщение слишком длинное. Сократите его и отправьте ещё раз.")

        # Serialize each user's turn so simultaneous Telegram updates cannot race
        # the persistent conversation history.
        user_lock = self._user_locks.get(context.telegram_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[context.telegram_id] = user_lock
        async with user_lock:
            await self._store.append(
                context.telegram_id,
                ConversationMessage(role="user", content=text),
            )
            results: list[ToolResult] = []
            for _ in range(self._max_tool_rounds + 1):
                history = await self._store.load(context.telegram_id)
                try:
                    turn = await asyncio.wait_for(
                        self._model.respond(
                            context=context,
                            messages=history,
                            tools=enabled_tools(self._allowed_tools),
                        ),
                        timeout=self._model_timeout_seconds,
                    )
                except Exception:
                    log.exception("Support model request failed; request_id=%s", context.request_id)
                    return EngineReply(
                        "Не удалось обработать сообщение сейчас. Передаю обращение специалисту.",
                        handoff=True,
                        handoff_reason="model_unavailable",
                        tool_results=tuple(results),
                    )

                if not turn.tool_call:
                    answer = turn.text.strip()
                    if not answer:
                        return EngineReply(
                            "Не получилось подготовить ответ. Передаю обращение специалисту.",
                            handoff=True,
                            handoff_reason="empty_model_response",
                            tool_results=tuple(results),
                        )
                    await self._store.append(
                        context.telegram_id,
                        ConversationMessage(role="assistant", content=answer),
                    )
                    return EngineReply(answer, tool_results=tuple(results))

                try:
                    tool_name, arguments = validate_tool_call(turn.tool_call, self._allowed_tools)
                except ToolPolicyError as exc:
                    log.warning(
                        "Blocked support tool call; request_id=%s reason=%s",
                        context.request_id,
                        exc,
                    )
                    return EngineReply(
                        "Для надёжности передаю обращение специалисту.",
                        handoff=True,
                        handoff_reason="tool_policy_rejected",
                        tool_results=tuple(results),
                    )

                call_id = turn.tool_call.call_id or uuid.uuid4().hex
                await self._store.append(
                    context.telegram_id,
                    ConversationMessage(
                        role="assistant",
                        content="",
                        tool_name=tool_name.value,
                        tool_call_id=call_id,
                        tool_arguments=arguments,
                    ),
                )

                try:
                    result = await asyncio.wait_for(
                        self._backend.invoke(
                            context=context,
                            tool_name=tool_name.value,
                            arguments=arguments,
                        ),
                        timeout=self._tool_timeout_seconds,
                    )
                except TimeoutError:
                    result = ToolResult(
                        ok=False,
                        code="temporarily_unavailable",
                        message="Tool timed out",
                        needs_human=True,
                    )
                except Exception:
                    log.exception(
                        "Support backend tool failed; request_id=%s tool=%s",
                        context.request_id,
                        tool_name.value,
                    )
                    result = ToolResult(
                        ok=False,
                        code="internal_error",
                        message="Tool failed",
                        needs_human=True,
                    )

                results.append(result)
                await self._store.append(
                    context.telegram_id,
                    ConversationMessage(
                        role="tool",
                        content="",
                        tool_name=tool_name.value,
                        tool_call_id=call_id,
                        tool_result=result,
                    ),
                )
                if tool_name is ToolName.CREATE_SUPPORT_TICKET and result.ok:
                    return EngineReply(
                        "Передал обращение специалисту. Ответ придёт сюда, когда он проверит ситуацию.",
                        handoff=True,
                        handoff_reason="ticket_created",
                        tool_results=tuple(results),
                    )
                if result.needs_human:
                    return EngineReply(
                        "Эту ситуацию должен проверить специалист. Передаю обращение.",
                        handoff=True,
                        handoff_reason=result.code,
                        tool_results=tuple(results),
                    )

            return EngineReply(
                "Проверка заняла больше шагов, чем ожидалось. Передаю обращение специалисту.",
                handoff=True,
                handoff_reason="tool_round_limit",
                tool_results=tuple(results),
            )
