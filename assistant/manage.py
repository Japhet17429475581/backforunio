"""Run from backend: python -m assistant.manage import|status|list|activate|deactivate."""
import argparse
import asyncio
from datetime import date
from database.connection import init_db,close_db,get_pool
from .store import import_corpus


async def run(args):
    if args.command == 'activate-bundled' and not args.reviewed:
        raise ValueError('Owner approval for current bundled editions requires --reviewed')
    await init_db()
    try:
        if args.command == 'activate-bundled':
            print('Owner-approved bundled documents:',await import_corpus(approve_current=True))
            return
        if args.command == 'import':
            print('Imported documents:',await import_corpus())
            return
        async with get_pool().acquire() as conn:
            if args.command == 'status':
                import json
                rows = await conn.fetch('''SELECT
                    CASE WHEN file_name LIKE 'unio-contact-%' AND file_name <> 'unio-contact-without-email.txt'
                         THEN 'contacts'
                         WHEN file_name LIKE 'unio-qa-%' THEN 'scenarios'
                         WHEN file_name = 'unio-contact-without-email.txt' THEN 'contact_notes'
                         ELSE 'documents' END AS category,
                    count(*) AS imported,
                    count(*) FILTER (WHERE reviewed AND active
                        AND (effective_from IS NULL OR effective_from <= CURRENT_DATE)
                        AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)) AS available,
                    count(*) FILTER (WHERE NOT reviewed) AS unreviewed,
                    count(*) FILTER (WHERE NOT active) AS inactive
                    FROM unio_assistant.documents GROUP BY 1 ORDER BY 1''')
                print(json.dumps({'event': 'assistant_knowledge_status',
                                  'categories': [dict(row) for row in rows]}))
                return
            if args.command == 'list':
                for row in await conn.fetch('SELECT file_name,title,reviewed,active,review_basis,source_url,effective_from,effective_to FROM unio_assistant.documents ORDER BY file_name'):
                    print(dict(row))
                return
            async with conn.transaction():
                row = await conn.fetchrow('SELECT id FROM unio_assistant.documents WHERE file_name=$1 FOR UPDATE',args.file)
                if row is None:
                    raise ValueError('Import this document first')
                if args.command == 'activate':
                    if not args.reviewed:
                        raise ValueError('Check original, OCR and applicable edition, then supply --reviewed')
                    if args.until and args.since and args.until < args.since:
                        raise ValueError('Invalid effective dates')
                    if args.replaces:
                        old = await conn.fetchval('SELECT id FROM unio_assistant.documents WHERE file_name=$1',args.replaces)
                        if old is None or old == row['id']:
                            raise ValueError('Invalid replaced document')
                        await conn.execute('UPDATE unio_assistant.documents SET active=false WHERE id=$1',old)
                    await conn.execute('''UPDATE unio_assistant.documents SET reviewed=true,active=true,
                        reviewed_at=now(),review_basis='manual',effective_from=$2,effective_to=$3 WHERE id=$1''',row['id'],args.since,args.until)
                else:
                    await conn.execute('UPDATE unio_assistant.documents SET active=false WHERE id=$1',row['id'])
            print('Document state updated')
    finally:
        await close_db()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['import','status','list','activate','deactivate','activate-bundled'])
    parser.add_argument('--file')
    parser.add_argument('--reviewed',action='store_true')
    parser.add_argument('--since',type=date.fromisoformat)
    parser.add_argument('--until',type=date.fromisoformat)
    parser.add_argument('--replaces')
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as error:
        import json
        from server_logging import error_details
        print(json.dumps({'event':'assistant_management_failed',**error_details(error)}))
        raise SystemExit(1) from None
