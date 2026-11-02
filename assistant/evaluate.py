"""Read retrieval results after operator activation; no calls to DeepSeek."""
import asyncio
import json
from database.connection import init_db,close_db
from .store import retrieve, ROOT

CASES = [
 ('При каких условиях полагается соц стипендия?','9283.pdf',{13,14,15}),
 ('Какие документы нужны для государственной социальной стипендии?','9283.pdf',{13,14,15}),
 ('Можно ли получать социальную стипендию в академическом отпуске?','9283.pdf',{14,15}),
 ('Что нужно для повышенной академической стипендии?','9283.pdf',set(range(6,14))),
 ('Как заселиться в общежитие?','3295.pdf',None),
 ('Какие права и обязанности у иностранного студента?','3764.pdf',None),
]


async def main():
    docs={x['file']:x['sha256'] for x in json.loads((ROOT/'data/corpus.json').read_text(encoding='utf-8'))}
    await init_db()
    hits=0
    try:
        for question,file,pages in CASES:
            sources=await retrieve(question)
            hit=any(x['document_id']==docs[file] and (pages is None or x['page'] in pages) for x in sources)
            hits+=hit
            print(json.dumps({'question':question,'expected_source_present':hit,
                'sources':[(x['title'],x['page']) for x in sources]},ensure_ascii=False))
        print(json.dumps({'retrieval_hits':hits,'cases':len(CASES)}))
    finally:
        await close_db()
    return 0 if hits==len(CASES) else 1


if __name__=='__main__':
    try:
        result=asyncio.run(main())
    except Exception as error:
        from server_logging import error_details
        print(json.dumps({'event':'assistant_evaluation_failed',**error_details(error)}))
        result=1
    raise SystemExit(result)
