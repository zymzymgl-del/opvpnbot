"""SQLite conversation and human-support inbox storage."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from support_bot.core.ports import ConversationStore
from support_bot.core.types import ConversationMessage, ToolResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT NOT NULL DEFAULT '',
    tool_name TEXT,
    tool_call_id TEXT,
    tool_arguments_json TEXT,
    tool_result_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_user_time
    ON conversation_messages(telegram_id, id DESC);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')) DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_user_status
    ON tickets(telegram_id, status, id DESC);
CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    sender TEXT NOT NULL CHECK (sender IN ('user', 'admin', 'system')),
    telegram_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SupportStore(ConversationStore):
    """Own the support bot's DB; it never opens or mutates the OPVPN database."""

    def __init__(self, path: Path, *, history_limit: int = 40, retention_days: int = 30) -> None:
        if history_limit < 4 or retention_days < 1:
            raise ValueError("History limit and retention must be positive")
        self.path = path
        self.history_limit = history_limit
        self.retention_days = retention_days
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._purge_expired()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SupportStore is not connected")
        return self._db

    async def _purge_expired(self) -> None:
        border = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        closed_border = (datetime.now(UTC) - timedelta(days=180)).isoformat(timespec="seconds")
        db = self._connection()
        async with self._lock:
            await db.execute("DELETE FROM conversation_messages WHERE created_at < ?", (border,))
            await db.execute(
                "DELETE FROM ticket_messages WHERE ticket_id IN "
                "(SELECT id FROM tickets WHERE status = 'closed' AND updated_at < ?)",
                (closed_border,),
            )
            await db.execute(
                "DELETE FROM tickets WHERE status = 'closed' AND updated_at < ?", (closed_border,)
            )
            await db.commit()

    async def load(self, telegram_id: int) -> list[ConversationMessage]:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                "SELECT role, content, tool_name, tool_call_id, tool_arguments_json, tool_result_json "
                "FROM conversation_messages "
                "WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
                (telegram_id, self.history_limit),
            )
            rows = await cursor.fetchall()
        messages: list[ConversationMessage] = []
        for row in reversed(rows):
            raw_result = row["tool_result_json"]
            result = None
            if raw_result:
                obj = json.loads(raw_result)
                result = ToolResult(
                    ok=bool(obj["ok"]),
                    code=str(obj["code"]),
                    message=str(obj["message"]),
                    data=obj.get("data", {}),
                    needs_human=bool(obj.get("needs_human", False)),
                )
            messages.append(ConversationMessage(
                role=row["role"], content=row["content"],
                tool_name=row["tool_name"], tool_call_id=row["tool_call_id"],
                tool_arguments=json.loads(row["tool_arguments_json"]) if row["tool_arguments_json"] else None,
                tool_result=result,
            ))
        return messages

    async def append(self, telegram_id: int, message: ConversationMessage) -> None:
        if telegram_id <= 0:
            raise ValueError("telegram_id must be positive")
        if message.role not in {"user", "assistant", "tool"}:
            raise ValueError("Unsupported conversation role")
        result_json = None
        arguments_json = None
        if message.tool_arguments is not None:
            arguments_json = json.dumps(dict(message.tool_arguments), ensure_ascii=False)
        if message.tool_result is not None:
            result_json = json.dumps({
                "ok": message.tool_result.ok,
                "code": message.tool_result.code,
                "message": message.tool_result.message[:1000],
                "data": dict(message.tool_result.data),
                "needs_human": message.tool_result.needs_human,
            }, ensure_ascii=False)
        db = self._connection()
        async with self._lock:
            await db.execute(
                "INSERT INTO conversation_messages(telegram_id, role, content, tool_name, "
                "tool_call_id, tool_arguments_json, tool_result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (telegram_id, message.role, message.content[:5000], message.tool_name,
                 message.tool_call_id, arguments_json, result_json, _now()),
            )
            await db.execute(
                "DELETE FROM conversation_messages WHERE telegram_id = ? AND id NOT IN "
                "(SELECT id FROM conversation_messages WHERE telegram_id = ? ORDER BY id DESC LIMIT ?)",
                (telegram_id, telegram_id, self.history_limit),
            )
            await db.commit()

    async def clear(self, telegram_id: int) -> None:
        db = self._connection()
        async with self._lock:
            await db.execute("DELETE FROM conversation_messages WHERE telegram_id = ?", (telegram_id,))
            await db.commit()

    async def add_user_message(self, telegram_id: int, text: str) -> tuple[int, bool]:
        """Append to the latest open ticket or create one; return (ticket_id, created)."""
        db = self._connection()
        now = _now()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT id FROM tickets WHERE telegram_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                (telegram_id,),
            )
            ticket = await cursor.fetchone()
            created = ticket is None
            if created:
                cursor = await db.execute(
                    "INSERT INTO tickets(telegram_id, status, created_at, updated_at) VALUES (?, 'open', ?, ?)",
                    (telegram_id, now, now),
                )
                ticket_id = int(cursor.lastrowid)
            else:
                ticket_id = int(ticket["id"])
                await db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
            await db.execute(
                "INSERT INTO ticket_messages(ticket_id, sender, telegram_id, text, created_at) "
                "VALUES (?, 'user', ?, ?, ?)",
                (ticket_id, telegram_id, text[:4000], now),
            )
            await db.commit()
        return ticket_id, created

    async def open_ticket_for_user(self, telegram_id: int) -> int | None:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                "SELECT id FROM tickets WHERE telegram_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                (telegram_id,),
            )
            row = await cursor.fetchone()
        return int(row["id"]) if row else None

    async def add_system_note(self, ticket_id: int, text: str) -> None:
        db = self._connection()
        now = _now()
        async with self._lock:
            await db.execute(
                "INSERT INTO ticket_messages(ticket_id, sender, telegram_id, text, created_at) "
                "VALUES (?, 'system', 0, ?, ?)",
                (ticket_id, text[:2000], now),
            )
            await db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
            await db.commit()

    async def create_ai_handoff(self, telegram_id: int, category: str, summary: str) -> int:
        history = await self.load(telegram_id)
        user_text = next((m.content for m in reversed(history) if m.role == "user"), "")
        ticket_id, _created = await self.add_user_message(
            telegram_id, user_text or "Пользователь обратился в поддержку."
        )
        await self.add_system_note(
            ticket_id,
            "Сводка ИИ (нужно проверить специалисту): "
            f"[{category[:80]}] {summary[:1500]}\n"
            "Диагностика аккаунта не подключена и не выполнялась.",
        )
        return ticket_id

    async def ticket_messages(self, ticket_id: int, limit: int = 12) -> list[aiosqlite.Row]:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                "SELECT sender, telegram_id, text, created_at FROM ticket_messages "
                "WHERE ticket_id = ? ORDER BY id DESC LIMIT ?", (ticket_id, limit)
            )
            rows = await cursor.fetchall()
        return list(reversed(rows))

    async def ticket_user(self, ticket_id: int) -> int | None:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute("SELECT telegram_id FROM tickets WHERE id = ?", (ticket_id,))
            row = await cursor.fetchone()
        return int(row["telegram_id"]) if row else None

    async def add_admin_reply(self, ticket_id: int, admin_id: int, text: str) -> int | None:
        db = self._connection()
        now = _now()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT telegram_id FROM tickets WHERE id = ? AND status = 'open'", (ticket_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return None
            user_id = int(row["telegram_id"])
            await db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
            await db.execute(
                "INSERT INTO ticket_messages(ticket_id, sender, telegram_id, text, created_at) "
                "VALUES (?, 'admin', ?, ?, ?)",
                (ticket_id, admin_id, text[:4000], now),
            )
            await db.commit()
        return user_id

    async def close_ticket(self, ticket_id: int) -> int | None:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                "UPDATE tickets SET status = 'closed', updated_at = ? "
                "WHERE id = ? AND status = 'open' RETURNING telegram_id",
                (_now(), ticket_id),
            )
            row = await cursor.fetchone()
            await db.commit()
        return int(row["telegram_id"]) if row else None

    async def erase_user_data(self, telegram_id: int) -> None:
        db = self._connection()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM ticket_messages WHERE ticket_id IN "
                "(SELECT id FROM tickets WHERE telegram_id = ?)", (telegram_id,)
            )
            await db.execute("DELETE FROM tickets WHERE telegram_id = ?", (telegram_id,))
            await db.execute("DELETE FROM conversation_messages WHERE telegram_id = ?", (telegram_id,))
            await db.commit()

    async def open_tickets(self, limit: int = 20) -> list[aiosqlite.Row]:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                "SELECT id, telegram_id, created_at, updated_at FROM tickets "
                "WHERE status = 'open' ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
            return await cursor.fetchall()
