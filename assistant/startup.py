"""Import the bundled corpus after database migrations, before serving requests."""
import asyncio

from server_logging import log_event
from . import config
from .store import import_corpus, CorpusJSONError


corpus_status = {'status': 'disabled'}


async def prepare_corpus():
    if not config.ENABLED:
        return
    if config.TEST_MODE:
        log_event('assistant_test_mode_enabled',code='request_quotas_disabled')
    corpus_status.update(status='importing')
    log_event('assistant_import_starting')
    try:
        async with asyncio.timeout(120):
            count=await import_corpus()
    except Exception as error:
        corpus_status.update(status='error')
        # The import transaction rolls back. Existing indexed documents remain
        # usable, and an assistant update must not stop the rest of UNIO.
        log_event('assistant_import_failed', level=40, error=error)
        if isinstance(error, CorpusJSONError):
            names = {'corpus.json': 'primary', 'additional_corpus.json': 'additional',
                     'activation.json': 'activation'}
            name = names.get(error.file_name, 'unknown')
            log_event(f'assistant_json_{name}_line_{error.line}_column_{error.column}', level=40)
    else:
        if isinstance(count,int):
            log_event(f'assistant_corpus_documents_{count}')
        try:
            from database.connection import get_pool
            from .store import ACTIVE
            async with get_pool().acquire() as conn:
                active = await conn.fetchval(f'SELECT count(*) FROM unio_assistant.documents d WHERE {ACTIVE}')
            corpus_status.update(status='ready' if active else 'empty', documents=active)
        except Exception as error:
            corpus_status.update(status='unknown')
            log_event('assistant_readiness_failed', level=30, error=error)
        log_event('assistant_import_complete')
