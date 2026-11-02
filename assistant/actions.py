"""Read-only app actions; no model calls, model-generated SQL or URLs."""
import re
from datetime import date, datetime, time, timedelta, timezone
from database.connection import get_pool

MOSCOW = timezone(timedelta(hours=3))
NEWS = re.compile(r'новост|\bnews\b', re.I)
PERSONAL_GRADES = re.compile(r'что\s+у\s+меня\s+по\s|как\s+у\s+меня\s+(?:по|с)\s|'
    r'(?:мои|моя|мой|my).{0,30}(?:балл|оценк|успеваем|срез|grades?|scores?)|'
    r'(?:покажи|show|check).{0,25}(?:успеваем|оценк|балл|grades?|scores?)',re.I)
REQUESTS = re.compile(
    r'\b(?:где|как|хочу|можно|закажи|заказать|оформи(?:ть)?|получить|открой|открыть|перейти|перейди|подать)\b.{0,65}(?:справк|заявлен)|'
    r'(?:справк|заявлен).{0,45}(?:заказать|оформить|открыть|подать)|'
    r'^\s*справки\s+и\s+заявления[?!.\s]*$|'
    r'(?:where|how|order|request|open|submit).{0,55}(?:certificate|application)', re.I)
MONTHS = [('январ','january'),('феврал','february'),('март','march'),
    ('апрел','april'),('мая','may'),('июня','june'),('июля','july'),
    ('август','august'),('сентябр','september'),('октябр','october'),
    ('ноябр','november'),('декабр','december')]


def news_date(question, now=None):
    """None = latest, False = unclear date, otherwise Moscow calendar day."""
    q=question.lower()
    today=(now or datetime.now(MOSCOW)).date()
    for word,shift in [('позавчера',-2),('day before yesterday',-2),
                        ('вчера',-1),('yesterday',-1),('сегодня',0),('today',0)]:
        if word in q:return today+timedelta(days=shift)
    iso=re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b',q)
    dotted=re.search(r'\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b',q)
    try:
        if iso:return date(*map(int,iso.groups()))
        if dotted:
            day,month,year=dotted.groups()
            return date(int(year) if year else today.year,int(month),int(day))
        for month,names in enumerate(MONTHS,1):
            for name in names:
                match=re.search(r'\b(\d{1,2})\s+'+name+r'\w*(?:\s+(\d{4}))?',q)
                if match:return date(int(match[2]) if match[2] else today.year,month,int(match[1]))
        if re.search(r'\d|завтра|tomorrow|за\s+(?:дат|день)|last week|недел|месяц|прошл|понедельник|вторник|среду|четверг|пятниц|суббот|воскресень',q):
            if not re.search(r'(?:последни\w*\s+[1-3]|[1-3]\s+последни|latest\s+[1-3]|[1-3]\s+latest)',q):
                return False
    except ValueError:return False
    return None


async def news_answer(question):
    target=news_date(question)
    if target is False:return {'status':'news_choose_date'}
    plural=bool(re.search(r'последние|последних|latest\s+[2-3]|[2-3]\s+latest',question,re.I))
    limit=3 if target is not None or plural else 1
    async with get_pool().acquire() as conn:
        if target is None:
            rows=await conn.fetch('''SELECT id,title,description,content,image_url,source_url,published_at
                FROM news ORDER BY published_at DESC NULLS LAST,id DESC LIMIT $1''',limit+1)
        else:
            start=datetime.combine(target,time.min,tzinfo=MOSCOW)
            rows=await conn.fetch('''SELECT id,title,description,content,image_url,source_url,published_at
                FROM news WHERE published_at >= $1 AND published_at < $2
                ORDER BY published_at DESC,id DESC LIMIT $3''',start,start+timedelta(days=1),limit+1)
    items=[]
    for row in rows[:limit]:
        content=row['content'] or row['description'] or ''
        items.append(dict(id=row['id'],title=row['title'][:500],
            description=(row['description'] or '')[:1000],content=content[:12000],
            content_truncated=not bool(row['content']) or len(content)>12000,image_url=(row['image_url'] or '')[:2000],
            source_url=(row['source_url'] or '')[:2000],
            published_at=row['published_at'].isoformat() if row['published_at'] else None))
    return dict(status='news',date=target.isoformat() if target else None,
                items=items,has_more=len(rows)>limit)
