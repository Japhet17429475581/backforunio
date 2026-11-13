"""Minimal consent evidence, retention and logs without request/user values."""
from __future__ import annotations

import asyncio
import json
import logging
from server_logging import PrivateLogFilter, configure_private_logging
import re
import asyncpg
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import UUID
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict
from database.connection import get_pool
from config import USER_RETENTION_DAYS, SCHEDULE_RETENTION_DAYS

DOCUMENTS = json.loads(Path(__file__).with_name('legal_versions.json').read_text(encoding='utf-8'))
DOCUMENT_HISTORY = json.loads(Path(__file__).with_name('legal_versions_history.json').read_text(encoding='utf-8'))


class ConsentRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: UUID
    document: str = Field(max_length=32)
    version: str = Field(max_length=80)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    accepted: bool
    occurred_at: datetime
    origin: Literal["user", "session_end", "version_update"] = "user"


def validate_consents(records: list[ConsentRecord], *, required: bool = False, allow_legacy: bool = False):
    latest = {}
    for record in records:
        doc = DOCUMENTS.get(record.document)
        matches_current = doc is not None and record.version == doc['version'] and record.sha256 == doc['sha256']
        matches_legacy = allow_legacy and any(
            record.version == prior['version'] and record.sha256 == prior['sha256']
            for prior in DOCUMENT_HISTORY.get(record.document, [])
        )
        if not matches_current and not matches_legacy:
            raise HTTPException(422, detail={'code': 'legal_version_mismatch'})
        if record.occurred_at.tzinfo is None or record.occurred_at > datetime.now(timezone.utc) + timedelta(minutes=10):
            raise HTTPException(422, detail={'code': 'invalid_consent_time'})
        previous = latest.get(record.document)
        if previous is None or previous.occurred_at <= record.occurred_at:
            latest[record.document] = record
    if required and any(name not in latest or not latest[name].accepted for name in ('terms', 'personal_data')):
        raise HTTPException(422, detail={'code': 'consent_required'})


async def save_consents(conn, user_id: int, records: list[ConsentRecord]):
    for record in records:
        await conn.execute('''INSERT INTO user_consents
            (event_id,user_id,document,version,document_sha256,accepted,occurred_at,origin)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (user_id,event_id) DO NOTHING''',
            record.event_id, user_id, record.document, record.version,
            record.sha256, record.accepted, record.occurred_at, record.origin)


class RetentionFailure(Exception):
    def __init__(self, stage, error):
        super().__init__('retention_failed')
        self.stage = stage
        self.error = error


def retention_failure_code(failure):
    """Only local stage names and database protocol codes; never exception text."""
    error = failure.error
    state = getattr(error, 'sqlstate', None)
    if not isinstance(state, str) or not re.fullmatch(r'[A-Z0-9]{5}', state):
        state = 'none'
    reasons = {
        '42501': 'permission_denied', '42P01': 'missing_table',
        '42703': 'missing_column', '23503': 'foreign_key',
        '42883': 'undefined_operator_or_function', '42804': 'datatype_mismatch',
        '57014': 'query_cancelled', '40P01': 'deadlock',
        '40001': 'serialization_failure', '53300': 'too_many_connections',
    }
    kind = ('postgres' if isinstance(error, asyncpg.PostgresError) else
            'timeout' if isinstance(error, TimeoutError) else
            'connection' if isinstance(error, (ConnectionError, OSError)) else
            'interface' if isinstance(error, asyncpg.InterfaceError) else 'unexpected')
    reason = reasons.get(state, 'connection_error' if state.startswith('08') else kind)
    return f'retention_failed_stage_{failure.stage}_type_{kind}_sqlstate_{state}_reason_{reason}'


retention_status = {}


async def cleanup_personal_data():
    stages = [
        ('web_sessions', [('DELETE FROM web_portal_sessions WHERE expires_at<now()', ())]),
        ('web_push', [("DELETE FROM web_push_subscriptions WHERE updated_at<now()-interval '30 days'", ()),
                      ("DELETE FROM web_push_deliveries WHERE created_at<now()-interval '10 days'", ())]),
        ('assistant', [("DELETE FROM unio_assistant.daily_usage WHERE day<CURRENT_DATE-31", ()),
                       ("DELETE FROM unio_assistant.budget WHERE CASE WHEN period LIKE 'day:%' THEN substring(period from 5)::date<CURRENT_DATE-31 WHEN period LIKE 'month:%' THEN (substring(period from 7)||'-01')::date<CURRENT_DATE-62 ELSE false END", ())]),
        ('users', [("DELETE FROM users WHERE COALESCE(token_last_used_at,token_created_at,created_at)<now()-($1::integer*interval '1 day')", (USER_RETENTION_DAYS,))]),
        ('schedule', [('DELETE FROM schedule_lessons WHERE lesson_date < CURRENT_DATE - $1::integer', (SCHEDULE_RETENTION_DAYS,))]),
    ]
    first_failure = None
    stage = 'acquire'
    try:
        async with get_pool().acquire() as conn:
            for stage, statements in stages:
                try:
                    async with conn.transaction():
                        for sql, args in statements:
                            await conn.execute(sql, *args)
                    retention_status[stage] = {'status': 'ok', 'last_success': datetime.now(timezone.utc).isoformat()}
                except Exception as error:
                    previous = retention_status.get(stage, {})
                    retention_status[stage] = {**previous, 'status': 'error'}
                    failure = RetentionFailure(stage, error)
                    logging.getLogger('unio').warning(retention_failure_code(failure))
                    first_failure = first_failure or failure
    except Exception as error:
        raise RetentionFailure(stage, error) from error
    if first_failure:
        raise first_failure


async def retention_loop():
    while True:
        try:
            await cleanup_personal_data()
        except RetentionFailure as failure:
            logging.getLogger('unio').warning(retention_failure_code(failure))
        except Exception:
            logging.getLogger('unio').warning('retention_failed_stage_unknown_type_unexpected')
        await asyncio.sleep(3600)
