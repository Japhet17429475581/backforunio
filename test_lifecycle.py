"""Resource cleanup regressions; no real database or university requests."""
import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from database import connection
import redis_db
import main


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_migration_discards_pool_and_allows_retry(self):
        pool = Mock(close=AsyncMock())
        with patch.object(connection, '_pool', None), \
                patch.object(connection.asyncpg, 'create_pool', AsyncMock(return_value=pool)), \
                patch.object(connection, 'create_tables', AsyncMock(side_effect=RuntimeError('migration'))):
            with self.assertRaises(RuntimeError):
                await connection.init_db()
            pool.close.assert_awaited_once()
            self.assertIsNone(connection._pool)

    async def test_failed_redis_ping_discards_client(self):
        client = Mock(ping=AsyncMock(side_effect=ConnectionError()), aclose=AsyncMock())
        with patch.object(redis_db, '_client', None), \
                patch.object(redis_db.redis, 'from_url', return_value=client):
            with self.assertRaises(ConnectionError):
                await redis_db.init_redis()
            client.aclose.assert_awaited_once()
            self.assertIsNone(redis_db._client)

    async def test_partial_startup_releases_databases(self):
        close_db, close_redis = AsyncMock(), AsyncMock()
        with patch.object(main, 'init_db', AsyncMock()), \
                patch.object(main, 'prepare_corpus', AsyncMock()), \
                patch.object(main, 'init_redis', AsyncMock(side_effect=ConnectionError())), \
                patch.object(main, 'close_db', close_db), \
                patch.object(main, 'close_redis', close_redis):
            with self.assertRaises(ConnectionError):
                async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
                    self.fail('Failed startup must not yield')
        close_db.assert_awaited_once()
        close_redis.assert_awaited_once()

    async def test_failed_assistant_close_still_cancels_workers_and_closes_databases(self):
        workers = []

        async def worker(*args):
            task = asyncio.current_task()
            workers.append(task)
            await asyncio.Event().wait()

        close_db, close_redis = AsyncMock(), AsyncMock()
        assistant = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('close')))
        with ExitStack() as patches:
            for name, value in {
                'init_db': AsyncMock(), 'prepare_corpus': AsyncMock(),
                'init_redis': AsyncMock(), 'close_db': close_db,
                'close_redis': close_redis, 'Assistant': Mock(return_value=assistant),
                '_news_sync_loop': worker, 'retention_loop': worker,
                'web_push_loop': worker,
            }.items():
                patches.enter_context(patch.object(main, name, value))
            with self.assertRaises(RuntimeError):
                async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
                    await asyncio.sleep(0)
        self.assertEqual(len(workers), 3)
        self.assertTrue(all(task.cancelled() for task in workers))
        close_db.assert_awaited_once()
        close_redis.assert_awaited_once()
