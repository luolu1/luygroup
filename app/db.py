"""Database engine, session factory, schema bootstrap and seed data."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator

from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_config
from .entitlements import ADVANCED_FEATURES
from .models import AIProvider, Base, ConsoleUser, SubscriptionPlan, utcnow
from .web.auth import hash_console_token, new_console_token

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_init_lock = asyncio.Lock()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_config().resolve_database_url()
        _engine = create_async_engine(url, echo=False, pool_pre_ping=True)

        if url.startswith("sqlite"):
            @event.listens_for(_engine.sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session."""
    async with session_scope() as session:
        yield session


def _upgrade_sqlite_schema(conn) -> None:  # type: ignore[no-untyped-def]
    """Apply small additive migrations for existing SQLite databases.

    ``create_all`` only creates missing tables; it never adds columns to an
    existing table. Keep these migrations additive and idempotent so upgrades
    from older installs keep working without a migration framework.
    """
    if conn.dialect.name != "sqlite":
        return
    columns = {
        "keywords": {
            "reply_text": "TEXT DEFAULT ''",
        },
        "members": {
            "is_member": "BOOLEAN DEFAULT 1",
            "mute_on_rejoin": "BOOLEAN DEFAULT 0",
        },
        "warnings": {
            "action": "TEXT DEFAULT 'warn'",
            "code": "TEXT DEFAULT ''",
            "content": "TEXT DEFAULT ''",
            "source": "TEXT DEFAULT ''",
            "review_status": "TEXT DEFAULT ''",
            "review_note": "TEXT DEFAULT ''",
            "reviewed_by": "BIGINT",
            "reviewed_at": "DATETIME",
        },
        "verifications": {
            "failed_attempts": "INTEGER DEFAULT 0",
        },
        "console_users": {
            "token_value": "VARCHAR(128)",
            "token_expires_at": "DATETIME",
            "balance_cents": "INTEGER NOT NULL DEFAULT 0",
        },
        "ai_providers": {
            "owner_user_id": "INTEGER",
        },
        "global_blacklist": {
            "owner_user_id": "INTEGER",
        },
        "reports": {
            "review_status": "TEXT DEFAULT 'pending'",
            "reviewed_by": "BIGINT",
            "reviewed_at": "DATETIME",
            "review_note": "VARCHAR(512) DEFAULT ''",
        },
    }
    for table, additions in columns.items():
        existing = {
            str(row[1])
            for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue
        for name, ddl in additions.items():
            if name not in existing:
                try:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                except OperationalError as exc:
                    # The bot and web lifespans initialize the DB concurrently.
                    # If the other task won the race, the column already exists.
                    if "duplicate column name" not in str(exc).lower():
                        raise

    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_ai_providers_owner_user_id "
        "ON ai_providers (owner_user_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_global_blacklist_owner_user_id "
        "ON global_blacklist (owner_user_id)"
    )

    console_columns = {
        str(row[1])
        for row in conn.exec_driver_sql("PRAGMA table_info(console_users)").fetchall()
    }
    if "console_path" in console_columns:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(
            "CREATE TABLE console_users_new ("
            "id INTEGER NOT NULL PRIMARY KEY, telegram_id BIGINT, username VARCHAR(128), "
            "display_name VARCHAR(255), token_hash VARCHAR(64) NOT NULL, "
            "token_value VARCHAR(128), token_expires_at DATETIME, role VARCHAR(24), "
            "is_active BOOLEAN, balance_cents INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME, last_login_at DATETIME)"
        )
        kept_columns = [
            "id", "telegram_id", "username", "display_name", "token_hash",
            "token_value", "token_expires_at", "role", "is_active",
            "balance_cents", "created_at", "last_login_at",
        ]
        names = ", ".join(kept_columns)
        conn.exec_driver_sql(
            f"INSERT INTO console_users_new ({names}) SELECT {names} FROM console_users"
        )
        conn.exec_driver_sql("DROP TABLE console_users")
        conn.exec_driver_sql("ALTER TABLE console_users_new RENAME TO console_users")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_console_users_telegram_id "
            "ON console_users (telegram_id)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_console_users_token_hash "
            "ON console_users (token_hash)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_console_users_token_expires_at "
            "ON console_users (token_expires_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_console_users_role ON console_users (role)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_console_users_is_active "
            "ON console_users (is_active)"
        )
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    # Backfill the new warning metadata for records created before the columns
    # existed. This is additive and safe to run on every startup.
    warning_columns = {
        str(row[1])
        for row in conn.exec_driver_sql("PRAGMA table_info(warnings)").fetchall()
    }
    keyword_columns = {
        str(row[1])
        for row in conn.exec_driver_sql("PRAGMA table_info(keyword_hits)").fetchall()
    }
    if not {
        "code",
        "source",
        "content",
        "review_status",
        "created_at",
        "group_id",
        "user_id",
    } <= warning_columns:
        return
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_warnings_code ON warnings (code)"
    )
    conn.exec_driver_sql(
        "UPDATE warnings SET code = strftime('%Y%m%d%H%M', created_at) "
        "WHERE (code IS NULL OR code = '') AND created_at IS NOT NULL"
    )
    conn.exec_driver_sql(
        "UPDATE warnings SET source = CASE "
        "WHEN reason LIKE 'AI 判定为广告%' THEN 'ai' ELSE 'manual' END "
        "WHERE source IS NULL OR source = ''"
    )
    conn.exec_driver_sql(
        "UPDATE warnings SET review_status = 'pending' "
        "WHERE source = 'ai' AND (review_status IS NULL OR review_status = '')"
    )
    if {"content", "created_at", "source", "group_id", "user_id"} <= keyword_columns:
        # Older AI warnings did not store the original message. Recover it from
        # the keyword-hit audit row written at the same moment when possible.
        conn.exec_driver_sql(
            """
            UPDATE warnings
            SET content = (
                SELECT kh.content FROM keyword_hits AS kh
                WHERE kh.group_id = warnings.group_id
                  AND kh.user_id = warnings.user_id
                  AND kh.source = 'ai'
                  AND kh.created_at IS NOT NULL
                  AND ABS(julianday(kh.created_at) - julianday(warnings.created_at)) < 0.0001
                LIMIT 1
            )
            WHERE source = 'ai'
              AND (content IS NULL OR content = '')
              AND EXISTS (
                SELECT 1 FROM keyword_hits AS kh
                WHERE kh.group_id = warnings.group_id
                  AND kh.user_id = warnings.user_id
                  AND kh.source = 'ai'
                  AND kh.created_at IS NOT NULL
                  AND ABS(julianday(kh.created_at) - julianday(warnings.created_at)) < 0.0001
              )
            """
        )
    # If several legacy warnings happened in the same minute, append the numeric
    # row id to every row sharing that code so the display IDs stay unique.
    conn.exec_driver_sql(
        "UPDATE warnings SET code = code || '-' || id "
        "WHERE instr(code, '-') = 0 AND code IN ("
        "SELECT code FROM warnings GROUP BY code HAVING COUNT(*) > 1)"
    )


async def init_db() -> None:
    # Both the web lifespan and bot startup call this function.  Serialize the
    # bootstrap so two concurrent SQLite CREATE TABLE operations cannot race.
    async with _init_lock:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_upgrade_sqlite_schema)

        # Seed AI providers declared in config.json without overwriting edits
        # made in the web panel. The DB is the source of truth after first start.
        cfg = get_config()
        async with session_scope() as session:
            existing = {p.name for p in (await session.scalars(select(AIProvider))).all()}
            for seed in cfg.ai.providers:
                if seed.name in existing:
                    continue
                session.add(
                    AIProvider(
                        name=seed.name,
                        label=seed.label or seed.name,
                        base_url=seed.base_url,
                        api_key=seed.api_key,
                        model=seed.model,
                        priority=seed.priority,
                        enabled=seed.enabled,
                        builtin=seed.builtin,
                    )
                )

            # Seed the default subscription plan once. Plans are managed by the
            # superadmin and define group limits plus AI/sample quotas.
            has_plan = await session.scalar(select(SubscriptionPlan.id).limit(1))
            if has_plan is None:
                session.add(
                    SubscriptionPlan(
                        name="pro",
                        label="订阅版",
                        features=json.dumps(
                            sorted(ADVANCED_FEATURES), ensure_ascii=False
                        ),
                        group_limit=5,
                        ai_daily_quota=200,
                        sample_daily_quota=500,
                        enabled=True,
                    )
                )

            # Migrate configured bot administrators to independent console users.
            # The token is retained only for its 24-hour display window; login
            # validation still uses the hash.
            for telegram_id in cfg.bot.admin_ids:
                user = await session.scalar(
                    select(ConsoleUser).where(ConsoleUser.telegram_id == int(telegram_id))
                )
                if user is not None:
                    continue
                token = new_console_token()
                user = ConsoleUser(
                    telegram_id=int(telegram_id),
                    username="",
                    display_name=f"管理员 {telegram_id}",
                    token_hash=hash_console_token(token),
                    token_value=None,
                    token_expires_at=utcnow() + timedelta(minutes=15),
                    role="superadmin",
                )
                session.add(user)
                await session.flush()
                log.warning("控制台超管账号已创建，请私聊机器人发送 /token 获取一次性登录码。")


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
