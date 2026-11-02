import io
import json
import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from server_logging import PrivateLogFilter, SafeLogFormatter, SafeRequestLogMiddleware


class OperationalLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        handler = logging.StreamHandler(self.output)
        handler.addFilter(PrivateLogFilter())
        handler.setFormatter(SafeLogFormatter())
        for item in [patch.object(logging.getLogger('unio'), 'handlers', [handler]),
                     patch.object(logging.getLogger('unio'), 'level', logging.INFO),
                     patch.object(logging.getLogger('unio'), 'propagate', False)]:
            item.start()
            self.addCleanup(item.stop)

    async def test_access_logs_use_templates_not_query_headers_body_or_client_id(self):
        app = FastAPI()
        app.add_middleware(SafeRequestLogMiddleware)

        @app.post('/items/{identifier}')
        async def item(identifier: str):
            return {'ok': True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/items/synthetic-sensitive?token=synthetic-sensitive',
                json={'password': 'synthetic-sensitive'}, headers={'Authorization': 'synthetic-sensitive', 'X-Request-Id': 'synthetic-sensitive'})
        data = json.loads(self.output.getvalue())
        self.assertEqual(data['route'], '/items/{identifier}')
        self.assertEqual(data['status'], 200)
        self.assertEqual(data['level'], 'INFO')
        self.assertEqual(data['request_id'], response.headers['x-request-id'])
        self.assertEqual(len(data['request_id']), 32)
        self.assertNotIn('synthetic-sensitive', self.output.getvalue())

    async def test_failures_keep_type_and_source_location_without_message(self):
        app = FastAPI()
        app.add_middleware(SafeRequestLogMiddleware)

        @app.get('/broken')
        async def broken():
            raise ValueError('password=synthetic-sensitive')

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
            response = await client.get('/broken')
        data = json.loads(self.output.getvalue())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(data['status'], 500)
        self.assertEqual(data['error_type'], 'ValueError')
        self.assertTrue(any('broken' in frame for frame in data['frames']))
        self.assertNotIn('synthetic-sensitive', self.output.getvalue())
        self.assertNotIn('password', self.output.getvalue())

    async def test_diagnostic_receipt_is_authenticated_and_logs_only_codes(self):
        from main import app
        body = {'operation': 'requests_list', 'code': 'requests_unrecognized_page', 'platform': 'android', 'status': 200}
        with patch('diagnostics._user_from_token', new=AsyncMock(return_value={'id': 12})) as auth, \
             patch('diagnostics.enforce_rate_limit', new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                result = await client.post('/api/v1/diagnostics/portal', json=body, headers={'X-Unio-Token': 'synthetic-sensitive'})
                invalid = await client.post('/api/v1/diagnostics/portal', json={**body, 'html': 'synthetic-sensitive'})
                unknown = await client.post('/api/v1/diagnostics/portal', json={**body, 'code': 'synthetic-sensitive'})
            auth.assert_awaited_once_with('synthetic-sensitive')
        self.assertEqual(result.status_code, 204)
        self.assertEqual(result.content, b'')
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(unknown.status_code, 422)
        self.assertIn('portal_client_failure', self.output.getvalue())
        self.assertIn('requests_unrecognized_page', self.output.getvalue())
        self.assertNotIn('synthetic-sensitive', self.output.getvalue())
        self.assertNotIn('synthetic-sensitive', invalid.text)

    def test_non_application_logs_discard_messages_and_keep_exception_type(self):
        record = logging.LogRecord('httpx', logging.ERROR, 'file', 1, 'secret=%s', ('synthetic-sensitive',),
                                  (RuntimeError, RuntimeError('synthetic-sensitive'), None))
        PrivateLogFilter().filter(record)
        encoded = SafeLogFormatter().format(record)
        self.assertIn('RuntimeError', encoded)
        self.assertIn('dependency_event', encoded)
        self.assertNotIn('synthetic-sensitive', encoded)

    def test_portal_logout_link_is_supported_without_accepting_other_origins(self):
        from bs4 import BeautifulSoup
        from web_portal import has_portal_session
        self.assertTrue(has_portal_session(BeautifulSoup('<a data-method="post" href="/auth/logout">Выход</a>', 'html.parser')))
        self.assertFalse(has_portal_session(BeautifulSoup('<a href="https://other.test/auth/logout">Выход</a>', 'html.parser')))


if __name__ == '__main__':
    unittest.main()
