import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from news.parser import VspuNewsParser
from news.service import NewsService


class NewsHeadingParserTests(unittest.TestCase):
    def test_drupal_heading_outside_article_is_preserved(self):
        html='''<html><head><link rel="canonical" href="/news-article/42"></head><body>
            <h1 class="title">Настоящий заголовок новости</h1>
            <article class="node--type-vspu-news">
            <div class="field--name-field-vspu-article-text">Полный текст статьи.</div></article></body></html>'''
        result=VspuNewsParser().parse_article(html,'42')
        self.assertEqual(result['title'],'Настоящий заголовок новости')
        self.assertEqual(result['content'],'Полный текст статьи.')
        self.assertEqual(result['source_url'],'https://www.vspu.ru/news-article/42')

    def test_inner_heading_and_meta_fallback_are_supported(self):
        parser=VspuNewsParser()
        for html in ['<article class="node--type-vspu-news"><h1 class="title">Заголовок</h1></article>',
            '<meta property="og:title" content="Заголовок"><article class="node--type-vspu-news"></article>']:
            self.assertEqual(parser.parse_article(html,'42')['title'],'Заголовок')

    def test_list_uses_link_text_when_title_wrapper_is_missing(self):
        html='''<div class="view-vspu-allnews"><div class="view-content">
            <div class="views-view-responsive-grid__item"><a class="newstitle" href="/news-article/42">Заголовок</a></div>
            </div></div>'''
        self.assertEqual(VspuNewsParser().parse_list(html)[0]['title'],'Заголовок')


class NewsHeadingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_cache_respects_each_callers_page_size(self):
        rows = [{'id': index} for index in range(50)]
        cached = None

        async def read_cache(key):
            return cached

        async def write_cache(key, value, ttl):
            nonlocal cached
            cached = value

        service = NewsService()
        with patch('news.service.get_news', AsyncMock(return_value=rows)) as query, \
                patch('news.service.redis_get_json', side_effect=read_cache), \
                patch('news.service.redis_set_json', side_effect=write_cache):
            self.assertEqual(len(await service.get_list(limit=20)), 20)
            self.assertEqual(len(await service.get_list(limit=50)), 50)
            self.assertEqual(len(await service.get_list(limit=1)), 1)
        query.assert_awaited_once_with(limit=50, offset=0)

    async def test_later_pages_bypass_the_first_page_cache(self):
        service = NewsService()
        with patch('news.service.get_news', AsyncMock(return_value=[{'id': 21}])) as query, \
                patch('news.service.redis_get_json', AsyncMock()) as cached:
            self.assertEqual(await service.get_list(limit=1, offset=20), [{'id': 21}])
        cached.assert_not_awaited()
        query.assert_awaited_once_with(limit=1, offset=20)

    async def test_startup_sync_retries_a_held_lock_without_waiting_a_day(self):
        import main
        with patch.object(main.news_service,'sync_news',AsyncMock(side_effect=[{'skipped':True},asyncio.CancelledError])), \
             patch('main.asyncio.sleep',AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):await main._news_sync_loop()
        self.assertEqual([c.args[0] for c in sleep.await_args_list],[5,30])

    async def test_failed_startup_sync_retries_instead_of_leaving_titles_broken(self):
        import main
        with patch.object(main.news_service,'sync_news',AsyncMock(side_effect=[RuntimeError('synthetic'),asyncio.CancelledError])), \
             patch('main.asyncio.sleep',AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):await main._news_sync_loop()
        self.assertEqual([c.args[0] for c in sleep.await_args_list],[5,60])
    async def test_loaded_body_does_not_prevent_repair_of_blank_title(self):
        broken=dict(id=42,external_id='42',title='',content='Already cached body')
        full=dict(external_id='42',title='Restored title',description=None,content='Full body',
            image_url=None,source_url='https://www.vspu.ru/news-article/42',published_at=None)
        restored=dict(broken,title='Restored title')
        service=NewsService()
        with patch('news.service.get_news_by_id',AsyncMock(side_effect=[broken,restored])), \
             patch.object(service.parser,'get_article',return_value=full), \
             patch('news.service.save_news',AsyncMock()) as save, \
             patch('news.service.redis_delete',AsyncMock()):
            result=await service.get_article(42)
        self.assertEqual(result['title'],'Restored title')
        self.assertEqual(save.await_args.kwargs['title'],'Restored title')

    async def test_empty_parsed_title_never_overwrites_known_title(self):
        known=dict(id=42,external_id='42',title='Known headline',content=None)
        full=dict(external_id='42',title='  ',description=None,content='Full body',
            image_url=None,source_url='https://www.vspu.ru/news-article/42',published_at=None)
        service=NewsService()
        with patch('news.service.get_news_by_id',AsyncMock(return_value=known)), \
             patch.object(service.parser,'get_article',return_value=full), \
             patch('news.service.save_news',AsyncMock()) as save, \
             patch('news.service.redis_delete',AsyncMock()):
            await service.get_article(42)
        self.assertEqual(save.await_args.kwargs['title'],'Known headline')

    async def test_sync_repairs_existing_rows_instead_of_stopping_at_first_known_url(self):
        service=NewsService()
        item=dict(external_id='42',title='Restored headline',image_url=None,
                  source_url='https://www.vspu.ru/news-article/42',published_at=None)
        with patch('news.service.redis_try_lock',AsyncMock(return_value='synthetic')), \
             patch('news.service.redis_unlock',AsyncMock()),patch('news.service.redis_delete',AsyncMock()), \
             patch('news.service.get_news_missing_titles',AsyncMock(side_effect=[[{'id':42}],[]])), \
             patch('news.service.get_news_external_urls',AsyncMock(return_value={item['source_url']})), \
             patch('news.service.get_news_missing_content',AsyncMock(return_value=[])), \
             patch('news.service.prune_news',AsyncMock(return_value=0)), \
             patch.object(service.parser,'get_news',return_value=[item]) as parser, \
             patch('news.service.save_news',AsyncMock()) as save:
            await service.sync_news()
        self.assertEqual(parser.call_args.kwargs['known_urls'],set())
        self.assertEqual(save.await_args.kwargs['title'],'Restored headline')
