import json
from pathlib import Path
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import httpx
from privacy import DOCUMENTS, DOCUMENT_HISTORY, ConsentRecord, validate_consents
from test_privacy import database


def receipts(version_index=0):
    return [ConsentRecord(event_id=uuid4(), document=key, accepted=True,
                          occurred_at=datetime.now(timezone.utc),
                          **DOCUMENT_HISTORY[key][version_index])
            for key in ('terms', 'personal_data')]


class Beta1CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_old_receipts_are_explicitly_allowed_only_for_legacy_contracts(self):
        for index in range(2):
            old = receipts(index)
            validate_consents(old, required=True, allow_legacy=True)
            with self.assertRaises(HTTPException):
                validate_consents(old, required=True)  # New PWA login stays current-only.
            for field in ('version', 'sha256'):
                changed = old[0].model_copy(update={field: 'unknown' if field == 'version' else '0' * 64})
                with self.assertRaises(HTTPException):
                    validate_consents([changed, old[1]], required=True, allow_legacy=True)
            withdrawn = old[1].model_copy(update={'accepted': False})
            with self.assertRaises(HTTPException):
                validate_consents([old[0], withdrawn], required=True, allow_legacy=True)

    async def test_beta1_and_current_guest_payloads_keep_same_api_response(self):
        from main import app
        variants = [receipts(), receipts(1),
                    [record.model_copy(update=DOCUMENTS[record.document]) for record in receipts()]]
        pool, conn = database()
        with patch('auth_api.auth.get_pool', return_value=pool), \
             patch('auth_api.auth.group_directory.validate', new=AsyncMock(return_value='TEST')), \
             patch('auth_api.auth.enforce_rate_limit', new=AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                for records in variants:
                    response = await client.post('/api/v1/auth/guest', json={'group_code': 'TEST',
                        'consents': [r.model_dump(mode='json') for r in records]})
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertTrue(data['success'])
                    self.assertTrue(data['account_token'].startswith('unio_'))
                    self.assertEqual(data['user']['group_code'], 'TEST')
        # Only creation inserts a new user; historical evidence retains its own version.
        evidence = [call.args for call in conn.execute.call_args_list if 'INSERT INTO user_consents' in call.args[0]]
        self.assertEqual(evidence[0][4], DOCUMENT_HISTORY['terms'][0]['version'])
        self.assertEqual(evidence[-2][4], DOCUMENTS['terms']['version'])

    async def test_beta1_consent_sync_and_token_me_remain_compatible(self):
        from main import app
        pool, conn = database()
        user = {'id': 42, 'auth_mode': 'guest', 'group_code': 'TEST', 'full_name': None}
        with patch('auth_api.auth.get_pool', return_value=pool), \
             patch('auth_api.auth.get_user_by_token_hash', new=AsyncMock(return_value=user)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                synced = await client.put('/api/v1/auth/consents', headers={'X-Unio-Token': 'unio_existing_beta1'},
                    json={'consents': [r.model_dump(mode='json') for r in receipts()]})
                me = await client.get('/api/v1/auth/me', headers={'X-Unio-Token': 'unio_existing_beta1'})
                logout = await client.post('/api/v1/auth/logout', headers={'X-Unio-Token': 'unio_existing_beta1'})
        self.assertEqual(synced.status_code, 204)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['user']['id'], 42)
        self.assertTrue(logout.json()['logged_out'])


if __name__ == '__main__':
    unittest.main()
