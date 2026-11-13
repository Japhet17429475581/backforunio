import asyncio
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

import main
import privacy
from database.schedule import save_lessons
from groups import GroupDirectory
from redis_db import RedisUnavailable
from test_privacy import database


class AuditRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.student_cache.clear()
        main.teacher_cache.clear()

    async def test_different_groups_overlap_but_same_week_fetches_once(self):
        active = peak = calls = 0

        async def fetch(**kwargs):
            nonlocal active, peak, calls
            calls += 1
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
            return ''

        with patch('main.redis_get_json', AsyncMock(return_value=None)), \
                patch('main.redis_try_lock', AsyncMock(return_value='lock')), \
                patch('main.redis_unlock', AsyncMock()), \
                patch('main.redis_set_json', AsyncMock()), \
                patch('main.save_lessons', AsyncMock()), \
                patch('main.fetch_html', fetch), \
                patch.object(main.student_parser, 'parse', return_value=[]):
            await asyncio.gather(*(main.get_lessons(kind='group', value=group,
                target=date.today(), force_refresh=False) for group in ['A', 'A', 'B']))
        self.assertEqual(calls, 2)
        self.assertEqual(peak, 2)
        self.assertEqual(len(main._schedule_locks), 0)

    async def test_redis_outage_still_loads_public_schedule(self):
        with patch('main.redis_get_json', AsyncMock(return_value=None)), \
                patch('main.redis_try_lock', AsyncMock(side_effect=RedisUnavailable)), \
                patch('main.redis_set_json', AsyncMock()), \
                patch('main.redis_unlock', AsyncMock()) as unlock, \
                patch('main.save_lessons', AsyncMock()), \
                patch('main.fetch_html', AsyncMock(return_value='')) as upstream, \
                patch.object(main.student_parser, 'parse', return_value=[]):
            result = await main.get_lessons(kind='group', value='A', target=date.today(), force_refresh=False)
        self.assertEqual(result[1], 'portal')
        upstream.assert_awaited_once()
        unlock.assert_not_awaited()

    async def test_timeout_uses_old_memory_with_explicit_stale_source(self):
        today = date.today()
        iso = today.isocalendar()
        main.student_cache[('A', iso.year, iso.week)] = main.CacheEntry(datetime.now()-timedelta(hours=1), [])
        with patch('main._get_lessons', AsyncMock(side_effect=TimeoutError)):
            self.assertEqual((await main.get_lessons(kind='group', value='A', target=today, force_refresh=False))[1], 'stale')

    async def test_empty_week_deletes_whole_range_and_never_inserts(self):
        pool, conn = database()
        start = date.today()
        end = start + timedelta(days=6)
        with patch('database.schedule.get_pool', return_value=pool):
            await save_lessons(kind='group', value='A', lessons=[], start=start, end=end)
        conn.execute.assert_awaited_once()
        self.assertEqual(conn.execute.call_args.args[-2:], (start, end))

    async def test_removed_first_day_is_in_replacement_range(self):
        pool, conn = database()
        start = date.today()
        lesson = SimpleNamespace(date=start+timedelta(days=3), start='09:40', end='11:10',
                                 subject='Test', lesson_type=None, room='1')
        with patch('database.schedule.get_pool', return_value=pool):
            await save_lessons(kind='group', value='A', lessons=[lesson], start=start, end=start+timedelta(days=6))
        self.assertEqual(conn.execute.call_args_list[0].args[-2:], (start, start+timedelta(days=6)))

    async def test_cleanup_other_stages_commit_after_missing_push_table(self):
        pool, conn = database()

        async def execute(sql, *args):
            if 'web_push_subscriptions' in sql:
                raise RuntimeError('synthetic')

        conn.execute.side_effect = execute
        with patch('privacy.get_pool', return_value=pool):
            with self.assertRaises(privacy.RetentionFailure):
                await privacy.cleanup_personal_data()
        self.assertIn('schedule_lessons', conn.execute.call_args_list[-1].args[0])
        self.assertEqual(privacy.retention_status['schedule']['status'], 'ok')
        self.assertEqual(privacy.retention_status['web_push']['status'], 'error')

    async def test_durable_groups_survive_restart_without_portal(self):
        pool, conn = database()
        conn.fetchrow = AsyncMock(return_value={'groups': ['A-111'], 'saved_at': datetime.now(timezone.utc)})
        directory = GroupDirectory()
        with patch('groups.get_pool', return_value=pool), \
                patch.object(directory, '_fetch', AsyncMock(side_effect=httpx.ConnectError('offline'))):
            self.assertEqual(await directory.validate('a111'), 'A-111')
            self.assertTrue(directory.stale)
            with self.assertRaises(HTTPException) as error:
                await directory.validate('unknown')
            self.assertEqual(error.exception.status_code, 503)
            await directory._refresh_task

    async def test_limiter_local_fallback_is_bounded(self):
        import rate_limit
        rate_limit._local.clear()
        with patch('rate_limit.get_redis', side_effect=RuntimeError):
            await rate_limit.enforce_rate_limit(key='audit', limit=1, window_seconds=60)
            with self.assertRaises(HTTPException) as error:
                await rate_limit.enforce_rate_limit(key='audit', limit=1, window_seconds=60)
        self.assertEqual(error.exception.status_code, 429)

    async def test_manual_news_sync_requires_admin_secret(self):
        from news.router import news_sync
        with patch.dict('os.environ', {'ADMIN_SYNC_TOKEN': ''}):
            with self.assertRaises(HTTPException) as error:
                await news_sync(SimpleNamespace(headers={}))
        self.assertEqual(error.exception.status_code, 403)
