from __future__ import annotations

import asyncpg
from pathlib import Path

from config import (
    DATABASE_URL,
    POSTGRES_MAX_POOL_SIZE,
    POSTGRES_MIN_POOL_SIZE,
)

_pool: asyncpg.Pool | None = None


async def init_db() -> asyncpg.Pool:
    global _pool

    if _pool is not None:
        return _pool

    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=POSTGRES_MIN_POOL_SIZE,
        max_size=POSTGRES_MAX_POOL_SIZE,
        command_timeout=30,
    )

    try:
        await create_tables()
        async with _pool.acquire() as conn:
            async with conn.transaction():
                # Serialize schema changes across workers.
                await conn.execute('SELECT pg_advisory_xact_lock(742901)')
                for migration in sorted((Path(__file__).parent.parent / 'migrations').glob('*.sql')):
                    await conn.execute(migration.read_text(encoding='utf-8'))
    except BaseException:
        # A failed migration must not leave an apparently initialized pool.
        await close_db()
        raise

    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("PostgreSQL ещё не подключён")

    return _pool


async def create_tables() -> None:
    pool = get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE,
                email TEXT UNIQUE,
                group_code TEXT,
                full_name TEXT,
                auth_mode TEXT NOT NULL DEFAULT 'guest'
                    CHECK (auth_mode IN ('guest', 'portal')),
                account_token_hash TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE INDEX IF NOT EXISTS idx_users_group
                ON users(group_code);

            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS auth_mode TEXT NOT NULL DEFAULT 'guest';

            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS account_token_hash TEXT;

            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS token_created_at TIMESTAMPTZ;

            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS token_last_used_at TIMESTAMPTZ;

            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ;

            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS token_revoked_at TIMESTAMPTZ;

            CREATE UNIQUE INDEX IF NOT EXISTS uq_users_account_token_hash
                ON users(account_token_hash)
                WHERE account_token_hash IS NOT NULL;

            UPDATE users
            SET token_created_at = COALESCE(token_created_at, updated_at, now()),
                token_expires_at = COALESCE(
                    token_expires_at,
                    COALESCE(token_created_at, updated_at, now()) + interval '30 days'
                )
            WHERE account_token_hash IS NOT NULL
              AND token_revoked_at IS NULL;


            CREATE TABLE IF NOT EXISTS schedule_lessons (
                id BIGSERIAL PRIMARY KEY,

                kind TEXT NOT NULL
                    CHECK (kind IN ('group', 'teacher')),

                owner TEXT NOT NULL,
                lesson_date DATE NOT NULL,
                start_time TIME NOT NULL,
                end_time TIME NOT NULL,

                subject TEXT NOT NULL,
                lesson_type TEXT,

                teacher TEXT,
                group_code TEXT,
                room TEXT,

                source TEXT NOT NULL DEFAULT 'vspu_portal',

                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_lesson
                ON schedule_lessons (
                    kind,
                    owner,
                    lesson_date,
                    start_time,
                    end_time,
                    subject,
                    COALESCE(teacher, ''),
                    COALESCE(group_code, ''),
                    COALESCE(room, '')
                );

            CREATE INDEX IF NOT EXISTS idx_schedule_owner_date
                ON schedule_lessons(owner, lesson_date);

            CREATE INDEX IF NOT EXISTS idx_schedule_group_date
                ON schedule_lessons(group_code, lesson_date);


            CREATE TABLE IF NOT EXISTS news (
                id BIGSERIAL PRIMARY KEY,

                external_id TEXT UNIQUE,

                title TEXT NOT NULL,
                description TEXT,
                content TEXT,

                image_url TEXT,
                source_url TEXT UNIQUE NOT NULL,

                published_at TIMESTAMPTZ,

                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE INDEX IF NOT EXISTS idx_news_published
                ON news(published_at DESC NULLS LAST, id DESC);

            CREATE INDEX IF NOT EXISTS idx_schedule_kind_owner_date_time
                ON schedule_lessons(kind, owner, lesson_date, start_time);

            CREATE INDEX IF NOT EXISTS idx_schedule_room_date_time
                ON schedule_lessons(lesson_date, room, start_time, end_time)
                WHERE room IS NOT NULL;


            CREATE TABLE IF NOT EXISTS push_tokens (
                id BIGSERIAL PRIMARY KEY,

                user_id BIGINT
                    REFERENCES users(id)
                    ON DELETE CASCADE,

                token TEXT UNIQUE NOT NULL,
                platform TEXT NOT NULL,

                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE INDEX IF NOT EXISTS idx_push_tokens_user
                ON push_tokens(user_id);
            """
        )


async def close_db() -> None:
    global _pool

    if _pool is not None:
        pool = _pool
        _pool = None
        await pool.close()
