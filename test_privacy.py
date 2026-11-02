import hashlib
import io
import json
import logging
import asyncio
import asyncpg
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi import HTTPException
from privacy import ConsentRecord, DOCUMENTS, validate_consents, save_consents, cleanup_personal_data, PrivateLogFilter, RetentionFailure, retention_loop
from auth_api import auth


def receipt(document, accepted=True):
    return ConsentRecord(event_id=uuid4(), document=document, **DOCUMENTS[document],
                         accepted=accepted, occurred_at=datetime.now(timezone.utc))


class Context:
    def __init__(self, value): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *args): return False


def database():
    conn = Mock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=42)
    conn.transaction.return_value = Context(conn)
    pool = Mock()
    pool.acquire.return_value = Context(conn)
    return pool, conn


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    def test_required_consents_do_not_require_analytics(self):
        records = [receipt('terms'), receipt('personal_data')]
        validate_consents(records, required=True)
        with self.assertRaises(HTTPException): validate_consents(records[:1], required=True)
        records.append(receipt('personal_data', False))
        with self.assertRaises(HTTPException): validate_consents(records, required=True)

    def test_version_hash_and_time_are_validated(self):
        for change in [{'version': 'old'}, {'sha256': '0' * 64},
                       {'occurred_at': datetime.now()},
                       {'occurred_at': datetime.now(timezone.utc) + timedelta(days=1)}]:
            r = receipt('terms').model_copy(update=change)
            with self.assertRaises(HTTPException): validate_consents([r])

    async def test_deletion_uses_only_hash_and_parameterized_scope(self):
        pool, conn = database()
        with patch.object(auth, 'get_pool', return_value=pool):
            for _ in range(2):
                response = await auth.delete_account('unio_synthetic')
                self.assertEqual(response.status_code, 204)
        query, value = conn.execute.call_args.args
        self.assertIn('WHERE account_token_hash=$1', query)
        self.assertEqual(value, hashlib.sha256(b'unio_synthetic').hexdigest())
        conn.transaction.assert_called()
        schema = (Path(__file__).parent / 'migrations/001_privacy.sql').read_text(encoding='utf8')
        self.assertIn('REFERENCES users(id) ON DELETE CASCADE', schema)

    async def test_missing_token_cannot_delete(self):
        with patch.object(auth, 'get_pool') as pool:
            with self.assertRaises(HTTPException): await auth.delete_account(None)
            pool.assert_not_called()

    async def test_evidence_idempotency_is_scoped_to_account(self):
        _, conn = database()
        await save_consents(conn, 42, [receipt('analytics', False)])
        query, *args = conn.execute.call_args.args
        self.assertIn('ON CONFLICT (user_id,event_id)', query)
        self.assertEqual(args[1], 42)
        self.assertEqual(args[5], False)

    async def test_retention_removes_expired_rows_with_configured_boundaries(self):
        pool, conn = database()
        with patch('privacy.get_pool', return_value=pool): await cleanup_personal_data()
        self.assertEqual(conn.execute.await_count, 7)
        self.assertIn('web_portal_sessions', conn.execute.call_args_list[0].args[0])
        self.assertIn("interval '30 days'", conn.execute.call_args_list[1].args[0])
        self.assertIn("interval '10 days'", conn.execute.call_args_list[2].args[0])
        self.assertIn('daily_usage WHERE day<CURRENT_DATE-31', conn.execute.call_args_list[3].args[0])
        self.assertIn('unio_assistant.budget', conn.execute.call_args_list[4].args[0])
        self.assertIn('COALESCE(token_last_used_at', conn.execute.call_args_list[5].args[0])
        self.assertEqual(conn.execute.call_args_list[5].args[1], 90)
        self.assertIn('lesson_date < CURRENT_DATE', conn.execute.call_args_list[6].args[0])

    async def test_legacy_routes_unmounted_and_validation_does_not_echo_inputs(self):
        from main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for path in ['/api/v1/auth/portal', '/api/v1/portal/login', '/api/v1/portal/grades', '/api/v1/portal/requests']:
                result = await client.post(path, json={'password': 'synthetic-sensitive'})
                self.assertEqual(result.status_code, 404)
            result = await client.post('/api/v1/auth/guest', json={'group_code': {'password': 'synthetic-sensitive'}})
            self.assertEqual(result.status_code, 422)
            self.assertNotIn('synthetic-sensitive', result.text)

    async def test_retention_diagnostics_survive_filter_without_sensitive_values(self):
        for index, stage, error, reason in [
            (0, 'web_sessions', asyncpg.InsufficientPrivilegeError('synthetic-sensitive'), 'permission_denied'),
            (1, 'web_push', asyncpg.InsufficientPrivilegeError('synthetic-sensitive'), 'permission_denied'),
            (3, 'assistant', asyncpg.InsufficientPrivilegeError('synthetic-sensitive'), 'permission_denied'),
            (5, 'users', asyncpg.InsufficientPrivilegeError('synthetic-sensitive'), 'permission_denied'),
            (6, 'schedule', asyncpg.UndefinedColumnError('synthetic-sensitive'), 'missing_column'),
        ]:
            pool, conn = database()
            conn.execute.side_effect = [None] * index + [error]
            output = io.StringIO()
            handler = logging.StreamHandler(output)
            handler.addFilter(PrivateLogFilter())
            logger = logging.getLogger('unio')
            with patch('privacy.get_pool', return_value=pool), \
                 patch.object(logger, 'handlers', [handler]), \
                 patch.object(logger, 'propagate', False), \
                 patch('privacy.asyncio.sleep', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await retention_loop()
            logged = output.getvalue()
            self.assertIn('retention_failed_stage_' + stage, logged)
            self.assertIn('sqlstate_' + error.sqlstate, logged)
            self.assertIn('reason_' + reason, logged)
            self.assertNotIn('synthetic-sensitive', logged)
            self.assertNotIn('Traceback', logged)
            self.assertEqual(conn.execute.await_count, index + 1)

    async def test_retention_acquire_failure_has_safe_stage(self):
        with patch('privacy.get_pool', side_effect=RuntimeError('synthetic-sensitive')):
            with self.assertRaises(RetentionFailure) as caught:
                await cleanup_personal_data()
        self.assertEqual(caught.exception.stage, 'acquire')
        self.assertEqual(str(caught.exception), 'retention_failed')

    def test_logs_strip_values_and_exception_traceback(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.addFilter(PrivateLogFilter())
        record = logging.LogRecord('httpx', logging.ERROR, 'file', 1, 'token=%s', ('synthetic-sensitive',),
                                   (ValueError, ValueError('synthetic-sensitive'), None))
        handler.handle(record)
        self.assertNotIn('synthetic-sensitive', output.getvalue())
        self.assertIn('dependency_event', output.getvalue())

    def test_document_versions_match_bundled_assets(self):
        # The fixture keeps this contract test runnable in the standalone backend.
        # In the combined workspace also verify the actual Flutter asset.
        root = Path(__file__).resolve().parent
        candidates = [root / 'fixtures/legal_documents.json',
                      root.parent.parent / 'assets/legal/documents.json']
        self.assertTrue(candidates[0].is_file(), 'Missing legal contract fixture')
        for path in candidates:
            if path.is_file():
                self._check_document_versions(path)

    def _check_document_versions(self, path):
        docs = json.loads(path.read_text(encoding='utf8'))
        for key, doc in DOCUMENTS.items():
            self.assertEqual(doc['version'], docs[key]['version'])
            self.assertEqual(doc['sha256'], hashlib.sha256(docs[key]['text'].encode()).hexdigest())


if __name__ == '__main__': unittest.main()
