from __future__ import annotations

from database.connection import get_pool


async def get_news(
    *,
    limit: int = 20,
    offset: int = 0,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                id,
                external_id,
                title,
                description,
                image_url,
                source_url,
                published_at,
                created_at,
                updated_at
            FROM news
            ORDER BY published_at DESC NULLS LAST, id DESC
            LIMIT $1
            OFFSET $2
            """,
            limit,
            offset,
        )


async def get_news_external_urls(*, limit: int = 50) -> set[str]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source_url
            FROM news
            WHERE NULLIF(btrim(title),'') IS NOT NULL
            ORDER BY published_at DESC NULLS LAST, id DESC
            LIMIT $1
            """,
            limit,
        )
    return {row["source_url"] for row in rows if row["source_url"]}


async def get_news_missing_content(*, limit: int = 5):
    async with get_pool().acquire() as conn:
        return await conn.fetch('''SELECT id FROM news
            WHERE (content IS NULL OR content='' OR NULLIF(btrim(title),'') IS NULL) AND external_id IS NOT NULL
            ORDER BY published_at DESC NULLS LAST,id DESC LIMIT $1''',limit)


async def get_news_missing_titles(*, limit: int = 50):
    async with get_pool().acquire() as conn:
        return await conn.fetch('''SELECT id FROM news WHERE NULLIF(btrim(title),'') IS NULL
            ORDER BY published_at DESC NULLS LAST,id DESC LIMIT $1''',limit)


async def get_news_by_id(
    news_id: int,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT *
            FROM news
            WHERE id = $1
            """,
            news_id,
        )


async def get_news_by_external_id(
    external_id: str,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT *
            FROM news
            WHERE external_id = $1
            """,
            external_id,
        )


async def get_news_by_url(
    source_url: str,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT *
            FROM news
            WHERE source_url = $1
            """,
            source_url,
        )


async def save_news(
    *,
    external_id: str,
    title: str,
    description: str | None,
    content: str | None,
    image_url: str | None,
    source_url: str,
    published_at,
):
    pool = get_pool()

    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO news (
                external_id,
                title,
                description,
                content,
                image_url,
                source_url,
                published_at
            )
            VALUES (
                $1, $2, $3, $4,
                $5, $6, $7
            )
            ON CONFLICT (external_id)
            DO UPDATE SET
                title = COALESCE(NULLIF(btrim(EXCLUDED.title),''),news.title),
                description = COALESCE(NULLIF(EXCLUDED.description,''),news.description),
                content = COALESCE(NULLIF(EXCLUDED.content,''),news.content),
                image_url = COALESCE(NULLIF(EXCLUDED.image_url,''),news.image_url),
                source_url = EXCLUDED.source_url,
                published_at = COALESCE(EXCLUDED.published_at,news.published_at),
                updated_at = now()
            RETURNING *
            """,
            external_id,
            title,
            description,
            content,
            image_url,
            source_url,
            published_at,
        )


async def prune_news(*, keep: int = 20) -> int:
    """
    Оставляет только keep самых свежих новостей.
    Возвращает количество удалённых записей.
    """
    pool = get_pool()

    async with pool.acquire() as conn:
        result = await conn.fetchval(
            """
            WITH old_news AS (
                SELECT id
                FROM news
                ORDER BY published_at DESC NULLS LAST, id DESC
                OFFSET $1
            ),
            deleted AS (
                DELETE FROM news
                WHERE id IN (SELECT id FROM old_news)
                RETURNING id
            )
            SELECT count(*) FROM deleted
            """,
            keep,
        )

    return int(result or 0)
