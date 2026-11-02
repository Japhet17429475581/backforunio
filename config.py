from __future__ import annotations

import os


APP_HOST = os.getenv("HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", "8000"))

# PostgreSQL
# Формат:
# postgresql://USER:PASSWORD@HOST:PORT/DATABASE
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
POSTGRES_MIN_POOL_SIZE = int(os.getenv("POSTGRES_MIN_POOL_SIZE", "1"))
POSTGRES_MAX_POOL_SIZE = int(os.getenv("POSTGRES_MAX_POOL_SIZE", "10"))

# Redis
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# VSPU portal
PORTAL_URL = os.getenv(
    "PORTAL_URL",
    "https://portal.vspu.ru/schedule/students",
)
TEACHER_PORTAL_URL = os.getenv(
    "TEACHER_PORTAL_URL",
    "https://portal.vspu.ru/schedule/lecturers",
)
CACHE_TTL = int(os.getenv("CACHE_TTL", "86400"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15"))
NEWS_CACHE_TTL = int(os.getenv("NEWS_CACHE_TTL", "300"))
NEWS_SYNC_INTERVAL = int(os.getenv("NEWS_SYNC_INTERVAL", "86400"))
AUTH_TOKEN_TTL_DAYS = int(os.getenv("AUTH_TOKEN_TTL_DAYS", "30"))
USER_RETENTION_DAYS = max(31, int(os.getenv("USER_RETENTION_DAYS", "90")))
SCHEDULE_RETENTION_DAYS = max(7, int(os.getenv("SCHEDULE_RETENTION_DAYS", "90")))
