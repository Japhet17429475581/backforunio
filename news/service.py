from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from config import NEWS_CACHE_TTL
from database.news import (
    get_news,
    get_news_by_id,
    get_news_external_urls,
    get_news_missing_content,
    get_news_missing_titles,
    prune_news,
    save_news,
)
from news.parser import VspuNewsParser
from server_logging import log_event
from redis_db import redis_delete, redis_get_json, redis_set_json, redis_try_lock, redis_unlock

NEWS_LIMIT = 50
# v4 always stores a full first page, regardless of the caller's requested limit.
NEWS_CACHE_KEY = "news:list:v4"
NEWS_SYNC_LOCK = "lock:news:sync"


class NewsService:
    def __init__(self):
        self.parser = VspuNewsParser()

    async def sync_news(self):
        lock = await redis_try_lock(NEWS_SYNC_LOCK, ttl=120)
        if lock is None:
            return {"saved": 0, "deleted": 0, "total_kept": NEWS_LIMIT, "skipped": True}

        try:
            repair_titles=bool(await get_news_missing_titles(limit=NEWS_LIMIT))
            if repair_titles:log_event('news_titles_repair_starting')
            known_urls = await get_news_external_urls(limit=NEWS_LIMIT)
            items = await asyncio.to_thread(
                self.parser.get_news,
                max_items=NEWS_LIMIT,
                known_urls=set() if repair_titles else known_urls,
            )

            inserted = 0
            for item in items:
                existed = item["source_url"] in known_urls
                await save_news(
                    external_id=item["external_id"],
                    title=item["title"],
                    description=None,
                    content=None,
                    image_url=item["image_url"],
                    source_url=item["source_url"],
                    published_at=item["published_at"],
                )
                if not existed:
                    inserted += 1

            deleted = await prune_news(keep=NEWS_LIMIT)
            # Prepare fresh article text in the background, never in an AI call.
            # Keep the batch small so a cold database does not fetch 50 articles.
            for row in await get_news_missing_content(limit=5):
                try:
                    await self.get_article(row['id'])
                except Exception as exc:
                    log_event('news_article_prefetch_failed',level=30,error=exc)
            await redis_delete(NEWS_CACHE_KEY)
            if repair_titles:
                remaining=await get_news_missing_titles(limit=1)
                log_event('news_titles_repair_pending' if remaining else 'news_titles_repair_complete')

            return {
                "saved": inserted,
                "deleted": deleted,
                "total_kept": NEWS_LIMIT,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "skipped": False,
            }
        finally:
            await redis_unlock(NEWS_SYNC_LOCK, lock)

    async def get_list(self, limit: int = NEWS_LIMIT, offset: int = 0):
        limit = min(limit, NEWS_LIMIT)
        use_cache = offset == 0 and limit <= NEWS_LIMIT

        if use_cache:
            cached = await redis_get_json(NEWS_CACHE_KEY)
            if cached is not None:
                return cached[:limit]

        rows = await get_news(limit=NEWS_LIMIT if use_cache else limit, offset=offset)
        if use_cache:
            payload = [dict(row) for row in rows]
            await redis_set_json(NEWS_CACHE_KEY, payload, NEWS_CACHE_TTL)
            return payload[:limit]
        return rows

    async def get_article(self, news_id: int):
        article = await get_news_by_id(news_id)
        if article is None or (article['content'] and (article['title'] or '').strip()):
            return article
        external_id = article["external_id"]
        if not external_id:
            return article

        full_article = await asyncio.to_thread(self.parser.get_article, external_id)
        if not full_article:
            return article

        full_article['title']=(full_article.get('title') or '').strip() or article['title']
        await save_news(**full_article)
        await redis_delete(NEWS_CACHE_KEY)
        return await get_news_by_id(news_id)
