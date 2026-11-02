from __future__ import annotations

from database.connection import get_pool


async def save_push_token(
    *,
    user_id: int,
    token: str,
    platform: str,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO push_tokens (
                user_id,
                token,
                platform
            )
            VALUES ($1, $2, $3)
            ON CONFLICT (token)
            DO UPDATE SET
                user_id = EXCLUDED.user_id,
                platform = EXCLUDED.platform,
                updated_at = now()
            RETURNING *
            """,
            user_id,
            token,
            platform,
        )


async def get_user_push_tokens(
    user_id: int,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT *
            FROM push_tokens
            WHERE user_id = $1
            ORDER BY id
            """,
            user_id,
        )


async def delete_push_token(
    token: str,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM push_tokens
            WHERE token = $1
            """,
            token,
        )
