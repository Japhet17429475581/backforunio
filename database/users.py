from __future__ import annotations

from typing import Any

from .connection import get_pool
from config import AUTH_TOKEN_TTL_DAYS


async def create_user(
    *,
    auth_mode: str,
    group_code: str | None = None,
    full_name: str | None = None,
    email: str | None = None,
    telegram_id: int | None = None,
) -> int:
    if auth_mode not in {"guest", "portal"}:
        raise ValueError("Недопустимый auth_mode")

    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (
                telegram_id,
                email,
                group_code,
                full_name,
                auth_mode
            )
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            telegram_id,
            email,
            group_code,
            full_name,
            auth_mode,
        )

        return int(row["id"])


async def set_account_token_hash(
    user_id: int,
    token_hash: str | None,
) -> None:
    pool = get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET
                account_token_hash = $1::text,

                token_created_at = CASE
                    WHEN $1::text IS NULL
                        THEN token_created_at
                    ELSE now()
                END,

                token_last_used_at = NULL,

                token_expires_at = CASE
                    WHEN $1::text IS NULL
                        THEN NULL
                    ELSE now() + ($2::integer * interval '1 day')
                END,

                token_revoked_at = NULL,
                updated_at = now()

            WHERE id = $3::integer
            """,
            token_hash,
            AUTH_TOKEN_TTL_DAYS,
            user_id,
        )


async def get_user_by_token_hash(
    token_hash: str,
) -> dict[str, Any] | None:
    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                telegram_id,
                email,
                group_code,
                full_name,
                auth_mode,
                created_at,
                updated_at,
                token_created_at,
                token_last_used_at,
                token_expires_at,
                token_revoked_at
            FROM users
            WHERE account_token_hash = $1::text
              AND token_revoked_at IS NULL
              AND (
                  token_expires_at IS NULL
                  OR token_expires_at > now()
              )
            """,
            token_hash,
        )

        if not row:
            return None

        await conn.execute(
            """
            UPDATE users
            SET
                token_last_used_at = now(),
                updated_at = now()
            WHERE id = $1::integer
            """,
            row["id"],
        )

        return dict(row)


async def revoke_account_token(
    token_hash: str,
) -> bool:
    pool = get_pool()

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE users
            SET
                account_token_hash = NULL,
                token_revoked_at = now(),
                token_expires_at = NULL,
                updated_at = now()
            WHERE account_token_hash = $1::text
            """,
            token_hash,
        )

        return result.endswith("1")


async def update_user_profile(
    user_id: int,
    *,
    group_code: str | None = None,
    full_name: str | None = None,
) -> None:
    pool = get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET
                group_code = COALESCE($1::text, group_code),
                full_name = COALESCE($2::text, full_name),
                updated_at = now()
            WHERE id = $3::integer
            """,
            group_code,
            full_name,
            user_id,
        )


async def upgrade_user_to_portal(
    user_id: int,
    *,
    token_hash: str,
    group_code: str | None = None,
    full_name: str | None = None,
) -> None:
    pool = get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET
                auth_mode = 'portal',

                account_token_hash = $1::text,

                token_created_at = now(),

                token_last_used_at = NULL,

                token_expires_at =
                    now() + ($2::integer * interval '1 day'),

                token_revoked_at = NULL,

                group_code = COALESCE($3::text, group_code),

                full_name = COALESCE($4::text, full_name),

                updated_at = now()

            WHERE id = $5::integer
            """,
            token_hash,
            AUTH_TOKEN_TTL_DAYS,
            group_code,
            full_name,
            user_id,
        )
