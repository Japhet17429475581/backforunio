"""Operational logs with fixed fields, route templates and no request contents."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys
import time
import traceback
from uuid import uuid4


def error_details(error):
    """Source locations are useful; exception messages and locals are excluded."""
    kind = type(error).__name__
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', kind):
        kind = 'Exception'
    details = {'error_type': kind}
    state = getattr(error, 'sqlstate', None)
    if isinstance(state, str) and re.fullmatch(r'[A-Z0-9]{5}', state):
        details['sqlstate'] = state
    frames = []
    for frame in traceback.extract_tb(error.__traceback__)[-6:]:
        # Do not include source text, absolute paths, exception text or locals.
        filename = os.path.basename(frame.filename.replace('\\', '/'))
        function = frame.name
        if re.fullmatch(r'[\w.-]{1,80}', filename) and re.fullmatch(r'[\w<>.]{1,80}', function):
            frames.append(f'{filename}:{frame.lineno}:{function}')
    if frames:
        details['frames'] = frames
    return details


def _safe_details(values):
    if not isinstance(values, dict):
        return {}
    result = {}
    for key in ('status', 'duration_ms'):
        value = values.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 86_400_000:
            result[key] = value
    patterns = {
        'request_id': r'[a-f0-9]{32}',
        'method': r'GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|OTHER',
        'route': r'/[A-Za-z0-9_/{}/.-]{0,160}|unmatched',
        'error_type': r'[A-Za-z_][A-Za-z0-9_]{0,79}',
        'sqlstate': r'[A-Z0-9]{5}',
        'operation': r'login|requests_list|request_types|request_form|request_create|request_upload|grades|other',
        'code': r'[a-z][a-z0-9_]{0,79}',
        'platform': r'android|web|other',
        'page_kind': r'empty|login|requests|other',
        'version': r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?',
    }
    for key, pattern in patterns.items():
        value = values.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            result[key] = value
    frames = values.get('frames')
    if isinstance(frames, list):
        result['frames'] = [frame for frame in frames[-6:] if isinstance(frame, str)
                            and re.fullmatch(r'[\w.-]{1,80}:\d{1,8}:[\w<>.]{1,80}', frame)]
    return result


class PrivateLogFilter(logging.Filter):
    def filter(self, record):
        if record.name == 'unio':
            if record.args or not isinstance(record.msg, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,200}', record.msg):
                record.msg = 'application_event'
        else:
            # Uvicorn lifecycle logs contain process IDs and literal text only.
            lifecycle = {
                'Started server process [%d]': 'server_process_started',
                'Waiting for application startup.': 'server_starting',
                'Application startup complete.': 'server_ready',
                'Shutting down': 'server_stopping',
                'Waiting for application shutdown.': 'server_stopping',
                'Application shutdown complete.': 'server_stopped',
                'Finished server process [%d]': 'server_process_stopped',
            }
            record.msg = lifecycle.get(record.msg, 'dependency_event') if isinstance(record.msg, str) else 'dependency_event'
        details = _safe_details(getattr(record, 'safe_details', {}))
        if record.exc_info and isinstance(record.exc_info[1], BaseException):
            details.update(error_details(record.exc_info[1]))
        record.safe_details = details
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


class SafeLogFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            'time': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds'),
            'level': record.levelname,
            'event': record.msg,
            **getattr(record, 'safe_details', {}),
        }, ensure_ascii=False)


def log_event(event, *, level=logging.INFO, error=None, **details):
    if error is not None:
        details.update(error_details(error))
    logging.getLogger('unio').log(level, event, extra={'safe_details': _safe_details(details)})


def configure_private_logging():
    level = os.getenv('UNIO_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level, logging.INFO) if level in {'DEBUG', 'INFO', 'WARNING', 'ERROR'} else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeLogFormatter())
    handler.addFilter(PrivateLogFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger('unio').setLevel(level)
    logging.getLogger('unio').disabled = False
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'httpx', 'httpcore', 'asyncio'):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(level if name.startswith('uvicorn') else logging.WARNING)
    # Built-in access logs expose URL/query/IP. Our middleware logs templates.
    logging.getLogger('uvicorn.access').disabled = True


class SafeRequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        started = time.monotonic()
        identifier = uuid4().hex  # Never trust caller-supplied correlation IDs.
        scope['unio.request_id'] = identifier
        status = 500
        failure = None

        async def observed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                headers = [(key, value) for key, value in message.get('headers', []) if key.lower() != b'x-request-id']
                message = {**message, 'headers': headers + [(b'x-request-id', identifier.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            status = 499
            raise
        except Exception as error:
            failure = error
            raise
        finally:
            method = scope.get('method', '')
            if method not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}:
                method = 'OTHER'
            route = getattr(scope.get('route'), 'path', 'unmatched')
            log_event('http_request', request_id=identifier, method=method, route=route,
                      status=status, duration_ms=round((time.monotonic() - started) * 1000),
                      level=logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO,
                      error=failure)
