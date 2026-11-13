"""Bounded lexical retrieval. Reuses the archive's chunking/provenance checks."""
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from database.connection import get_pool
from .core import chunks, digest, query_text, normalize
from .config import CONTEXT_CHARS, USER_DAILY, DAY_BUDGET, MONTH_BUDGET
from .sources import public_source_url

ROOT = Path(__file__).parent
ACTIVE = '''d.active AND d.reviewed
 AND (d.effective_from IS NULL OR d.effective_from<=CURRENT_DATE)
 AND (d.effective_to IS NULL OR d.effective_to>=CURRENT_DATE)'''
FIELDS = 'c.*,d.title,d.file_name,d.source_url,d.effective_from,d.effective_to'
STOP = {'какие','каких','когда','можно','нужно','получить','пожалуйста','условиях','полагается','меня','есть','what','how','does','about','which'}


async def retrieve(question):
    q = query_text(question)
    # English UI queries still retrieve Russian originals, without another LLM call.
    translations = {'social':'социальная','scholarship':'стипендия','dormitory':'общежитие',
                    'dorm':'общежитие','move':'заселение','tuition':'обучение стоимость',
                    'certificate':'справка','academic':'академическая','leave':'отпуск',
                    'documents':'документы','foreign':'иностранный','student':'студент',
                    'admission':'прием','support':'поддержка','payment':'оплата',
                    'education':'образование','additional':'дополнительное','continuing':'дополнительное',
                    'retraining':'переподготовка','courses':'курсы','qualification':'квалификация'}
    q = re.sub(r'\b[a-z]+\b', lambda m: translations.get(m[0],m[0]), q.lower())
    terms = [x for x in re.findall(r'[а-яёa-z]{3,}', q.lower()) if x not in STOP][:18]
    if not terms:
        return []
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(f'''SELECT {FIELDS},
          ts_rank_cd(c.fts,websearch_to_tsquery('russian',$1))*4 +
          ts_rank_cd(c.fts,websearch_to_tsquery('russian',$2)) AS rank
          FROM unio_assistant.chunks c JOIN unio_assistant.documents d ON d.id=c.document_id
          WHERE {ACTIVE} AND c.fts @@ websearch_to_tsquery('russian',$2)
          ORDER BY rank DESC,c.id LIMIT 6''', q, ' OR '.join(terms))
        selected, seen, total = [], set(), 0
        def add(row):
            nonlocal total
            if row['id'] not in seen and total+len(row['text']) <= CONTEXT_CHARS:
                selected.append(dict(row)); seen.add(row['id']); total += len(row['text'])
        for row in rows[:4]:
            add(row)
        for row in rows[:3]:
            for neighbor in await conn.fetch(f'''SELECT {FIELDS}
              FROM unio_assistant.chunks c JOIN unio_assistant.documents d ON d.id=c.document_id
              WHERE {ACTIVE} AND c.document_id=$1 AND c.ordinal BETWEEN $2 AND $3
              ORDER BY c.ordinal''',row['document_id'],max(0,row['ordinal']-1),row['ordinal']+1):
                add(neighbor)
    return [dict(source_id=f'S{i}', document_id=x['document_id'], title=x['title'],
                 page=x['page'], locator=x['locator'], text=x['text'], ocr=x['ocr'],
                 source_url=public_source_url(x.get('source_url')))
            for i,x in enumerate(selected,1)]


async def reserve_budget(user_id, tokens, *, enforce_user_limit=True):
    """Reserve worst-case UTF-8-byte input + max output BEFORE calling provider.
    No refunds on failure: even ambiguous timeouts cannot exceed reserved budget.
    Shared across workers/restarts. The same lock order prevents deadlocks.
    """
    now = datetime.now(timezone(timedelta(hours=3)))
    day, month = now.date(), now.strftime('%Y-%m')
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            for period, limit in [('month:'+month, MONTH_BUDGET), ('day:'+day.isoformat(), DAY_BUDGET)]:
                await conn.execute('INSERT INTO unio_assistant.budget(period) VALUES($1) ON CONFLICT DO NOTHING',period)
                used = await conn.fetchval('SELECT reserved_tokens FROM unio_assistant.budget WHERE period=$1 FOR UPDATE', period)
                if used+tokens > limit:
                    raise BudgetExceeded()
                await conn.execute('UPDATE unio_assistant.budget SET reserved_tokens=reserved_tokens+$2 WHERE period=$1',period,tokens)
            if not enforce_user_limit:
                return
            await conn.execute('INSERT INTO unio_assistant.daily_usage(user_id,day) VALUES($1,$2) ON CONFLICT DO NOTHING',user_id,day)
            count = await conn.fetchval('SELECT calls FROM unio_assistant.daily_usage WHERE user_id=$1 AND day=$2 FOR UPDATE',user_id,day)
            if count >= USER_DAILY:
                raise BudgetExceeded()
            await conn.execute('UPDATE unio_assistant.daily_usage SET calls=calls+1 WHERE user_id=$1 AND day=$2',user_id,day)


class BudgetExceeded(Exception):
    pass


class CorpusJSONError(ValueError):
    """Safe diagnostic: bundled filename and coordinates, never document text."""
    def __init__(self, path, error):
        self.file_name = path.name
        self.line = error.lineno
        self.column = error.colno
        super().__init__('Invalid bundled knowledge JSON')


def read_corpus_json(path):
    # Editors/hosting panels may add a UTF-8 BOM. Accept it without changing
    # document text or silently repairing genuinely malformed JSON.
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as error:
        raise CorpusJSONError(path, error) from None


async def import_corpus(*, approve_current=False):
    """Import JSON text without requiring originals; changed text revokes review."""
    corpus = read_corpus_json(ROOT/'data/corpus.json')
    additional_path=ROOT/'data/additional_corpus.json'
    if additional_path.is_file():
        corpus+=read_corpus_json(additional_path)
    if len({d['sha256'] for d in corpus})!=len(corpus):
        raise ValueError('Duplicate corpus identifiers')
    approval_path=ROOT/'data/activation.json'
    approval=read_corpus_json(approval_path) if approval_path.is_file() and not approve_current else {}
    approved=approval.get('documents',{}) if approval.get('mode')=='owner_test_approval' else {}
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(742902)')
            for doc in corpus:
                name = doc['file']
                if not isinstance(name,str) or not name or name in ('.','..') or '/' in name or '\\' in name or ':' in name:
                    raise ValueError('Invalid corpus file')
                if not isinstance(doc['sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',doc['sha256']):
                    raise ValueError('Invalid document identifier')
                content = digest(json.dumps({'pages':doc['pages'],'title':doc['title']},ensure_ascii=False,sort_keys=True))
                previous = await conn.fetchval('SELECT content_hash FROM unio_assistant.documents WHERE id=$1',doc['sha256'])
                if previous != content:
                    await conn.execute('''INSERT INTO unio_assistant.documents(id,file_name,title,content_hash)
                  VALUES($1,$2,$3,$4) ON CONFLICT(id) DO UPDATE SET content_hash=$4,title=$3,
                  reviewed=false,active=false,reviewed_at=NULL,review_basis=NULL,approval_hash=NULL''',doc['sha256'],doc['file'],doc['title'],content)
                    await conn.execute('DELETE FROM unio_assistant.chunks WHERE document_id=$1',doc['sha256'])
                    for chunk in chunks(doc['pages']):
                        await conn.execute('''INSERT INTO unio_assistant.chunks
                     (id,document_id,ordinal,page,locator,text,ocr) VALUES($1,$2,$3,$4,$5,$6,$7)''',
                     doc['sha256']+':'+str(chunk['ordinal']),doc['sha256'],chunk['ordinal'],chunk['page'],
                     chunk['locator'],normalize(chunk['text']),chunk['ocr'])
                url=public_source_url(doc.get('source_page_url')) or public_source_url(doc.get('source_url'))
                await conn.execute('''UPDATE unio_assistant.documents SET source_url=$2
                    WHERE id=$1 AND source_url IS DISTINCT FROM $2''',doc['sha256'],url)
                if approve_current or approved.get(doc['sha256'])==content:
                    # Approval applies once to this exact edition. A later manual
                    # deactivate survives restarts; changed text needs new approval.
                    approval_hash=digest('owner_test_approval:'+content)
                    update='''UPDATE unio_assistant.documents SET
                        reviewed=true,active=true,reviewed_at=now(),review_basis='owner_test_approval',approval_hash=$2
                        WHERE id=$1'''
                    if not approve_current:
                        update+=' AND approval_hash IS DISTINCT FROM $2'
                    await conn.execute(update,doc['sha256'],approval_hash)
    return len(corpus)
