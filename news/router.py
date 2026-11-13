import os
import secrets
from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
)

from news.service import NewsService
from rate_limit import enforce_rate_limit


router = APIRouter(
    prefix="/api/v1/news",
    tags=["news"],
)

service = NewsService()


@router.get("")
async def news_list(
    limit: int = Query(
        20,
        ge=1,
        le=50,
    ),
    offset: int = Query(
        0,
        ge=0,
    ),
):
    rows = await service.get_list(
        limit=limit,
        offset=offset,
    )

    return {
        "success": True,
        "count": len(rows),
        "items": [
            {
                "id": row["id"],
                "external_id": row["external_id"],
                "title": row["title"],
                "image_url": row["image_url"],
                "source_url": row["source_url"],
                "published_at": row["published_at"],
            }
            for row in rows
        ],
    }


@router.get("/{news_id}")
async def news_article(
    news_id: int,
):
    article = await service.get_article(
        news_id
    )

    if article is None:
        raise HTTPException(
            status_code=404,
            detail="Новость не найдена",
        )

    return {
        "success": True,
        "item": {
            "id": article["id"],
            "external_id": article["external_id"],
            "title": article["title"],
            "description": article["description"],
            "content": article["content"],
            "image_url": article["image_url"],
            "source_url": article["source_url"],
            "published_at": article["published_at"],
        },
    }


@router.post("/sync")
async def news_sync(request: Request):
    expected = os.getenv('ADMIN_SYNC_TOKEN', '')
    if not expected or not secrets.compare_digest(request.headers.get('x-admin-token', ''), expected):
        raise HTTPException(403, detail={'code': 'admin_required'})
    await enforce_rate_limit(
        key=f"news:sync:{request.client.host if request.client else 'unknown'}",
        limit=1,
        window_seconds=300,
    )
    saved = await service.sync_news()

    return {
        "success": True,
        **saved,
    }
