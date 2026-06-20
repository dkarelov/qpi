from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncpg import Pool, Record

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass
class UserData:
    """Persistent support user record."""

    message_thread_id: int | None
    message_silent_id: int | None
    message_silent_mode: bool
    id: int
    full_name: str
    username: str | None
    state: str = "member"
    is_banned: bool = False
    language_code: str | None = None
    created_at: str = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S %Z")
    status: str = "open"

    def to_dict(self) -> dict:
        return asdict(self)


def validate_schema_name(schema: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(schema):
        raise ValueError(f"Invalid PostgreSQL schema name: {schema!r}")
    return schema


def table_name(schema: str, name: str) -> str:
    schema = validate_schema_name(schema)
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid PostgreSQL table name: {name!r}")
    return f"{schema}.{name}"


async def create_schema(pool: Pool, schema: str = "support_bot") -> None:
    """Create qpi support-bot tables in the isolated support schema."""
    users = table_name(schema, "users")
    ai_drafts = table_name(schema, "ai_drafts")
    conversations = table_name(schema, "conversations")
    async with pool.acquire() as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {validate_schema_name(schema)}")
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {users} (
                id BIGINT PRIMARY KEY,
                message_thread_id BIGINT,
                message_silent_id BIGINT,
                message_silent_mode BOOLEAN NOT NULL DEFAULT FALSE,
                full_name TEXT NOT NULL DEFAULT '',
                username TEXT,
                state TEXT NOT NULL DEFAULT 'member',
                is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                language_code TEXT,
                created_at TEXT,
                status TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        await conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS users_thread_idx "
            f"ON {users} (message_thread_id) WHERE message_thread_id IS NOT NULL"
        )
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ai_drafts} (
                user_id BIGINT PRIMARY KEY,
                text TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {conversations} (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(f"CREATE INDEX IF NOT EXISTS conversations_user_idx ON {conversations} (user_id, id)")


class RedisStorage:
    """PostgreSQL-backed repository; legacy class name retained for upstream imports."""

    CONV_MAX = 40

    def __init__(self, pool: Pool, schema: str = "support_bot") -> None:
        self.pool = pool
        self.schema = validate_schema_name(schema)

    def _table(self, name: str) -> str:
        return table_name(self.schema, name)

    @staticmethod
    def _row_to_user(row: Record) -> UserData:
        return UserData(
            message_thread_id=row["message_thread_id"],
            message_silent_id=row["message_silent_id"],
            message_silent_mode=row["message_silent_mode"],
            id=row["id"],
            full_name=row["full_name"],
            username=row["username"],
            state=row["state"],
            is_banned=row["is_banned"],
            language_code=row["language_code"],
            created_at=row["created_at"],
            status=row["status"],
        )

    async def get_by_message_thread_id(self, message_thread_id: int) -> UserData | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._table('users')} WHERE message_thread_id = $1",
                message_thread_id,
            )
        return None if row is None else self._row_to_user(row)

    async def get_user(self, id_: int) -> UserData | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {self._table('users')} WHERE id = $1", id_)
        return None if row is None else self._row_to_user(row)

    async def update_user(self, id_: int, data: UserData) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table("users")} (
                    id, message_thread_id, message_silent_id, message_silent_mode,
                    full_name, username, state, is_banned, language_code, created_at, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (id) DO UPDATE SET
                    message_thread_id = EXCLUDED.message_thread_id,
                    message_silent_id = EXCLUDED.message_silent_id,
                    message_silent_mode = EXCLUDED.message_silent_mode,
                    full_name = EXCLUDED.full_name,
                    username = EXCLUDED.username,
                    state = EXCLUDED.state,
                    is_banned = EXCLUDED.is_banned,
                    language_code = EXCLUDED.language_code,
                    created_at = EXCLUDED.created_at,
                    status = EXCLUDED.status
                """,
                id_,
                data.message_thread_id,
                data.message_silent_id,
                data.message_silent_mode,
                data.full_name,
                data.username,
                data.state,
                data.is_banned,
                data.language_code,
                data.created_at,
                data.status,
            )

    async def get_all_users_ids(self) -> list[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT id FROM {self._table('users')}")
        return [int(row["id"]) for row in rows]

    async def set_ai_draft(self, user_id: int, text: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._table('ai_drafts')} (user_id, text) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET text = EXCLUDED.text",
                user_id,
                text,
            )

    async def get_ai_draft(self, user_id: int) -> str | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                f"SELECT text FROM {self._table('ai_drafts')} WHERE user_id = $1",
                user_id,
            )

    async def clear_ai_draft(self, user_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {self._table('ai_drafts')} WHERE user_id = $1", user_id)

    async def append_conversation(self, user_id: int, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        text = text[:2000]
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._table('conversations')} (user_id, role, content) VALUES ($1, $2, $3)",
                user_id,
                role,
                text,
            )
            await conn.execute(
                f"""
                DELETE FROM {self._table("conversations")}
                WHERE user_id = $1 AND id NOT IN (
                    SELECT id FROM {self._table("conversations")} WHERE user_id = $1
                    ORDER BY id DESC LIMIT $2
                )
                """,
                user_id,
                self.CONV_MAX,
            )

    async def get_conversation(self, user_id: int, limit: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT role, content FROM {self._table('conversations')} WHERE user_id = $1 "
                "ORDER BY id DESC LIMIT $2",
                user_id,
                limit,
            )
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
