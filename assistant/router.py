import re
import asyncio
import json
from contextlib import suppress
from typing import Literal
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
from auth_api.auth import _user_from_token
from database.connection import get_pool
from redis_db import get_redis
from server_logging import log_event
from . import config
from .store import ROOT, ACTIVE

router = APIRouter(prefix='/api/v1/assistant',tags=['assistant'])


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=2,max_length=800)
    context_question: str = Field(default='',max_length=500)
    language: Literal['ru','en'] = 'ru'
    group: str | None = Field(default=None,max_length=100)
    allow_external: bool = False
    stream: bool = False


@router.post('/message')
async def message(data: Question,request: Request,response: Response,x_unio_token: str | None = Header(default=None)):
    response.headers['Cache-Control']='no-store, private'
    if not x_unio_token or not re.fullmatch(r'unio_[A-Za-z0-9_-]{20,240}',x_unio_token):
        raise HTTPException(401,detail={'code':'auth_required'})
    user = await _user_from_token(x_unio_token)
    if not config.ENABLED:
        return {'status':'not_configured'}
    # Quotas are disabled in the owner-requested closed-test mode.
    if not config.TEST_MODE:
        try:
            count = await get_redis().eval('''local n=redis.call('INCR',KEYS[1]);
          if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n''',1,f"assistant:rate:{user['id']}")
            if count>6:
                return {'status':'limit'}
        except Exception as exc:
            log_event('assistant_limiter_unavailable',level=30,error=exc)
            return {'status':'unavailable'}
    if not data.stream:
        return await request.app.state.assistant.answer(data,user,request.app.state.get_lessons)
    async def events():
        queue=asyncio.Queue(maxsize=8)
        async def emit(kind,payload):
            await queue.put((kind,payload))
        async def run():
            try:
                async with asyncio.timeout(65):
                    result=await request.app.state.assistant.answer(data,user,request.app.state.get_lessons,emit)
            except Exception as exc:
                log_event('assistant_stream_failed',level=30,error=exc)
                result={'status':'unavailable'}
            await emit('result',result)
        task=asyncio.create_task(run())
        try:
            yield 'event: status\ndata: {"status":"searching"}\n\n'
            while True:
                try:
                    kind,payload=await asyncio.wait_for(queue.get(),timeout=10)
                except TimeoutError:
                    yield ': keep-alive\n\n'
                    continue
                yield f'event: {kind}\ndata: {json.dumps(payload,ensure_ascii=False)}\n\n'
                if kind=='result':break
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):await task
    return StreamingResponse(events(),media_type='text/event-stream',headers={
        'Cache-Control':'no-store, private','X-Accel-Buffering':'no'})


@router.get('/sources/{document_id}')
async def source(document_id: str):
    if not re.fullmatch(r'[a-f0-9]{64}',document_id):
        raise HTTPException(404)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(f'SELECT file_name FROM unio_assistant.documents d WHERE {ACTIVE} AND id=$1',document_id)
    if not row:
        raise HTTPException(404)
    root = (ROOT/'data/originals').resolve()
    path = (root/row['file_name']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path,filename=path.name,headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
