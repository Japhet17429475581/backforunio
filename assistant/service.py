import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from . import config
from .core import validate_claims, query_text
from .store import retrieve, reserve_budget, BudgetExceeded
from .actions import NEWS, REQUESTS, PERSONAL_GRADES, news_answer
from .sources import source_card
from server_logging import log_event

SYSTEM = Path(__file__).with_name('prompt.txt').read_text(encoding='utf-8')
MOSCOW = timezone(timedelta(hours=3))
TOPIC = re.compile(r'вгспу|универ|вуз|студент|стипенд|общежит|общаг|обучен|образован|уч[её]б|деканат|справк|заявлен|зач[её]т|сесси|экзамен|поступ|при[её]м|академ|оплат|перевод|иностран|переподготов|квалификац|campus|universit|scholarship|dorm|student|tuition|certificate|admission|education',re.I)
# Reject clear unrelated tasks cheaply; absence of a keyword is NOT evidence of an unrelated question.
OFF_TOPIC = re.compile(r'рецепт|борщ|анекдот|гороскоп|криптовалют|биткоин|напиши.{0,20}(?:код|сочинение|стих)|recipe|horoscope|bitcoin|tell.{0,8}joke|write.{0,20}(?:code|essay|poem)',re.I)
INJECTION = re.compile(r'игнорируй|забудь.{0,25}(правил|инструкц)|системн.{0,15}промпт|ignore.{0,30}instruction|system prompt|pretend|jailbreak',re.I)
SENSITIVE = re.compile(r'unio_[\w-]{12,}|sk-[\w-]{12,}|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:пароль|password|token|токен)\s*[:=]|\+?\d[\d ()-]{9,}\d',re.I)


def schedule_date(question, now=None):
    """Deterministic, no provider or model-chosen parameters."""
    q = question.lower()
    if not re.search(r'расписани|\bпар[аыу]?\b|заняти|schedule|classes|lessons',q):
        return None
    today = (now or datetime.now(MOSCOW)).date()
    if 'послезавтра' in q or 'day after tomorrow' in q:
        return today+timedelta(days=2)
    if 'завтра' in q or 'tomorrow' in q:
        return today+timedelta(days=1)
    if 'сегодня' in q or 'today' in q:
        return today
    match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b',q)
    if match:
        try:
            return datetime.strptime(match[1],'%Y-%m-%d').date()
        except ValueError:
            return False
    days = [('понедельник','monday'),('вторник','tuesday'),('сред','wednesday'),('четверг','thursday'),('пятниц','friday'),('суббот','saturday'),('воскресень','sunday')]
    for i,names in enumerate(days):
        if any(x in q for x in names):
            delta = (i-today.weekday())%7
            if 'следующ' in q or 'next ' in q:
                delta = 7-today.weekday()+i
            return today+timedelta(days=delta)
    return False  # Ask for a date; never guess a whole week or an ambiguous date.


class Assistant:
    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(25,connect=5),
            limits=httpx.Limits(max_connections=config.PARALLEL,max_keepalive_connections=config.PARALLEL),
            follow_redirects=False)
        self.slots = asyncio.Semaphore(config.PARALLEL)

    async def close(self):
        await self.client.aclose()

    async def answer(self, data, user, loader, on_event=None):
        q = data.question.strip()
        if SENSITIVE.search(q) or SENSITIVE.search(data.context_question):
            return {'status':'sensitive'}
        if INJECTION.search(q):
            return {'status':'off_topic'}
        if PERSONAL_GRADES.search(q):
            return {'status':'grades_on_device'}
        if NEWS.search(q):
            try:
                return await news_answer(q)
            except Exception as exc:
                log_event('assistant_news_unavailable',level=30,error=exc)
                return {'status':'news_unavailable'}
        if REQUESTS.search(q):
            return {'status':'requests_help','action':'open_requests'}
        target = schedule_date(q)
        if target is not None:
            if on_event: await on_event('status',{'status':'schedule_loading'})
            if target is False:
                return {'status':'choose_date'}
            today = datetime.now(MOSCOW).date()
            if abs((target-today).days)>14:
                return {'status':'date_range'}
            group = data.group or user.get('group_code')
            if not group:
                return {'status':'choose_group'}
            # Selected group is public schedule data; no portal credentials involved.
            from groups import directory
            try:
                group = await directory.validate(group)
                lessons, source, cached_at = await loader(kind='group',value=group,target=target,force_refresh=False)
            except Exception as exc:
                log_event('assistant_schedule_unavailable',level=30,error=exc)
                return {'status':'schedule_unavailable'}
            day_lessons = [dict(date=x.date.isoformat(),start=x.start,end=x.end,subject=x.subject,
              lesson_type=x.lesson_type,teacher=x.teacher,room=x.room,group=group)
              for x in lessons if x.date == target]
            def minutes(s):
                try:
                    h,m = map(int,s.split(':')); return h*60+m
                except (ValueError,AttributeError):
                    return 1440
            day_lessons.sort(key=lambda x:(minutes(x['start']),minutes(x['end']),x['subject']))
            if re.search(r'перв\w*\s+пар|first\s+(?:class|lesson)',q,re.I):
                day_lessons=day_lessons[:1]
            return dict(status='schedule',date=target.isoformat(),group=group,lessons=day_lessons,
                        cache_source=source,cached_at=cached_at.isoformat() if cached_at else None)
        if OFF_TOPIC.search(q) and not TOPIC.search(query_text(q)):
            return {'status':'off_topic'}
        if not data.allow_external:
            return {'status':'permission_required'}
        if not config.KEY:
            return {'status':'not_configured'}
        if self.slots.locked():
            return {'status':'busy'}
        async with self.slots:
            if on_event: await on_event('status',{'status':'searching'})
            sources = await retrieve(query_text(q+' '+data.context_question))
            if not sources:
                return {'status':'insufficient','sources':[]}
            payload = dict(question=q,previous_question=data.context_question,language=data.language,
                           sources=[{k:v for k,v in s.items() if k!='source_url'} for s in sources])
            content = json.dumps(payload,ensure_ascii=False)
            # Byte bound intentionally conservative for mixed Russian / English and OCR.
            reserved = len((SYSTEM+content).encode('utf-8')) + config.MAX_OUTPUT + 256
            if not config.TEST_MODE:
                try:
                    await reserve_budget(user['id'],reserved)
                except BudgetExceeded:
                    return {'status':'limit'}
            try:
                if on_event: await on_event('status',{'status':'writing'})
                request_body = {
                    'model':config.MODEL,'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':content}],
                    'thinking':{'type':'disabled'},'temperature':0.1,'max_tokens':config.MAX_OUTPUT,
                    'response_format':{'type':'json_object'},'stream':on_event is not None}
                if on_event:
                    result = await self._stream(request_body,sources,on_event)
                else:
                    response = await self.client.post('https://api.deepseek.com/chat/completions',
                        headers={'Authorization':'Bearer '+config.KEY},json=request_body)
                    response.raise_for_status()
                    choice = response.json()['choices'][0]
                    if choice['finish_reason'] != 'stop':
                        raise ValueError('Incomplete response')
                    result = json.loads(choice['message']['content'])
                if result.get('status') == 'off_topic':
                    return {'status':'off_topic'}
                claims = validate_claims(result,sources)
                if not claims:
                    return {'status':'insufficient','sources':[]}
                if len(claims)>4 or any(len(c['text'])>500 or len(c['evidence'])>2 for c in claims):
                    raise ValueError('Unbounded claims')
                used = {e['source_id'] for c in claims for e in c['evidence']}
                # Public quotations only; don't send full retrieved context to the device.
                cards = [source_card(s) for s in sources if s['source_id'] in used]
                log_event('assistant_answered',code='grounded')
                return dict(status='answered',claims=claims,sources=cards)
            except (httpx.HTTPError,TimeoutError,ValueError,KeyError,IndexError,TypeError,AttributeError) as exc:
                log_event('assistant_provider_failed',level=30,error=exc)
                return {'status':'unavailable'}

    async def _stream(self, body, sources, on_event):
        """Emit only complete, citation-validated claims, never raw model tokens."""
        buffer, emitted, finish = '', 0, None
        async with asyncio.timeout(30):
            async with self.client.stream('POST','https://api.deepseek.com/chat/completions',
                    headers={'Authorization':'Bearer '+config.KEY},json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):continue
                    raw=line[5:].strip()
                    if raw=='[DONE]':break
                    event=json.loads(raw)
                    if not event.get('choices'):continue
                    choice=event['choices'][0]
                    finish=choice.get('finish_reason') or finish
                    buffer+=choice.get('delta',{}).get('content') or ''
                    if len(buffer)>24000:raise ValueError('Oversized response')
                    claims=partial_claims(buffer,sources)
                    if len(claims)>emitted:
                        emitted=len(claims)
                        used={e['source_id'] for c in claims for e in c['evidence']}
                        cards=[source_card(s) for s in sources if s['source_id'] in used]
                        await on_event('partial',dict(status='answered',claims=claims,sources=cards))
        if finish!='stop':raise ValueError('Incomplete response')
        return json.loads(buffer)


def partial_claims(buffer,sources):
    """Decode a real top-level JSON prefix, not JSON-looking text inside a quote."""
    decoder=json.JSONDecoder()
    def skip(index):
        while index<len(buffer) and buffer[index].isspace():index+=1
        return index
    try:
        i=skip(0)
        if buffer[i]!='{':return []
        i+=1
        status=None
        while True:
            i=skip(i)
            key,i=decoder.raw_decode(buffer,i)
            i=skip(i)
            if buffer[i]!=':':return []
            i=skip(i+1)
            if key=='claims':
                if status!='answered' or buffer[i]!='[':return []
                i+=1; claims=[]
                while len(claims)<4:
                    i=skip(i)
                    try:claim,end=decoder.raw_decode(buffer,i)
                    except json.JSONDecodeError:return claims
                    if not validate_claims({'status':'answered','claims':[claim]},sources):return []
                    if len(claim['text'])>500 or len(claim['evidence'])>2:return []
                    claims.append(claim);i=skip(end)
                    if i>=len(buffer) or buffer[i]!=',':return claims
                    i+=1
                return claims
            value,i=decoder.raw_decode(buffer,i)
            if key=='status':status=value
            i=skip(i)
            if buffer[i]!=',':return []
            i+=1
    except (ValueError,TypeError,IndexError,AttributeError):
        return []
