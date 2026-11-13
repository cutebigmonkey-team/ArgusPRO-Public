import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from browser_public import normalize_rows
from catalog_scope import select
from run import collect_source


class FacebookPublicTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_feed_returns_before_the_supervisors_40_second_timeout(self):
        import depth
        from facebook_feed import collect
        context=AsyncMock()
        budgets=[]
        async def elapsed(coroutine,timeout):
            coroutine.close()
            budgets.append(timeout)
            return 'time_budget'
        token=depth.bounded_poll.set(True)
        try:
            with patch('facebook_feed.context_for',return_value=context), \
                 patch('facebook_feed.asyncio.wait_for',side_effect=elapsed), \
                 patch('facebook_feed.time.monotonic',return_value=0):
                _,status,_=await collect(object(),{'platform':'facebook','url':'https://www.facebook.com/example'},30)
            self.assertEqual(status,'time_budget')
            self.assertTrue(budgets)
            self.assertTrue(all(0 < seconds < 40 for seconds in budgets))
            context.close.assert_awaited_once()
        finally:
            depth.bounded_poll.reset(token)

    def test_owned_media_attachment_survives_without_story_text_or_date(self):
        from facebook_feed import story_post
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        media='https://cdn.example/photo.jpg'
        story={'post_id':'123','url':source['url']+'/posts/123','attachments':[
            {'styles':{'attachment':{'media':{'image':{'uri':media}}}}}]}
        item=story_post(source,story)
        self.assertEqual((item['text'],item['published_at'],item['media_urls']),('',None,[media]))
    def test_native_story_text_date_and_feed_ownership(self):
        from facebook_feed import extract,story_post,combine
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        url=source['url']+'/posts/pfbidVerified'
        story={'post_id':'123','creation_time':1793602800,'comet_sections':{'content':{'story':{
            'post_id':'123','wwwURL':url,'message':{'text':'Повний текст допису'},
            'actors':[{'url':source['url'],'id':'55'}]}}}}
        feed={'timeline_list_feed_units':{'edges':[{'node':story}],'page_info':{'has_next_page':False}},
              'feedback':{'group_feed':{'edges':[{'node':{'post_id':'foreign'}}]}}}
        nodes,flags=extract(feed)
        self.assertEqual((len(nodes),flags),(1,[False]))
        item=story_post(source,nodes[0])
        self.assertEqual(item['platform_post_id'],'55_123')
        self.assertEqual(item['text'],'Повний текст допису')
        self.assertFalse(item['text_may_be_truncated'])
        self.assertTrue(item['published_at'].startswith('2026-11-02'))
        self.assertIsNone(story_post({**source,'url':'https://www.facebook.com/foreign'},story))
        self.assertIsNone(story_post(source,story,'facebook:789'))
        items={}
        combine(items,[item,{**item,'text':'Preview…','facebook_text_source':'unverified',
                             'text_may_be_truncated':True,'published_at':None}])
        self.assertEqual(items[item['canonical_key']]['text'],'Повний текст допису')
        self.assertEqual(len(extract({'path':['node','timeline_list_feed_units','edges',2],
                                      'data':{'node':story}})[0]),1)

    def test_posts_tab_exposes_multiple_owned_links_without_rendered_cards(self):
        from facebook_feed import owned_story_links
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        links=[source['url']+'/posts/123',source['url']+'/posts/new-update/456',
               source['url']+'/posts/123',source['url']+'/posts/333?comment_id=42',
               'https://www.facebook.com/foreign/posts/789']
        self.assertEqual(set(owned_story_links(source,links)),{'facebook:123','facebook:456'})

    async def test_verified_wall_permalink_can_supply_an_absolute_publication_date(self):
        from browser_public import wall_dates
        from types import SimpleNamespace
        import time
        source={'platform':'ok','url':'https://ok.ru/group/7'}
        url='https://ok.ru/publisher/topic/42'
        record={'canonical_key':'ok:42','post_url':url,'published_at':None}
        context=AsyncMock()
        page=context.new_page.return_value
        page.url=url
        page.goto.return_value=SimpleNamespace(status=200)
        from unittest.mock import Mock
        page.locator=Mock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value='Public topic')))
        page.evaluate.return_value='2026-01-01T06:30:00+00:00'
        progress={}
        with patch('browser_public.asyncio.sleep',new_callable=AsyncMock):
            await wall_dates(context,source,{'ok:42':record},progress,time.monotonic()+20)
            self.assertEqual(record['published_at'],'2026-01-01T06:30:00+00:00')
            record['published_at']=None
            await wall_dates(context,source,{'ok:42':record},progress,time.monotonic()+20)
            self.assertEqual(context.new_page.await_count,1)
            page.url='https://ok.ru/publisher/topic/99'
            await wall_dates(context,source,{'ok:42':record},{},time.monotonic()+20)
            self.assertIsNone(record['published_at'])

    async def test_public_browser_precedes_static_login_result(self):
        source = {'platform':'facebook','url':'https://www.facebook.com/example'}
        with patch.dict(os.environ, {'ARGUS_EMBED_COLLECTION':'1'}), patch('facebook_feed.collect',new_callable=AsyncMock) as read:
            read.return_value = ([{'canonical_key':'facebook:123'}],'ok',[{'tool':'facebook_public_feed'}])
            posts,status,attempts = await collect_source(object(),source,10)
        self.assertEqual((len(posts),status,attempts[0]['tool']),(1,'ok','facebook_public_feed'))

    async def test_real_browser_markup(self):
        if os.environ.get('ARGUS_FACEBOOK_DOM_TEST') != '1':
            self.skipTest('Chromium fixture runs on cloud runners')
        from playwright.async_api import async_playwright
        script = Path(__file__).with_name('facebook_cards.js').read_text(encoding='utf-8')
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=os.getenv('ARGUS_DOM_TEST_CHROME'))
            try:
                page = await browser.new_page()
                await page.set_content('''<base href="https://www.facebook.com/"><form>Log in</form>
                    <article><h3>Example</h3><a href="/example/posts/123">Date</a>
                    <div data-ad-preview="message">Публічний допис</div>
                    <div role="article"><p>Nested comment</p></div></article>
                    <article><a href="/example/posts/321">Like / Log in</a></article>
                    <article><a href="/foreign/posts/456">Other</a><p>Wrong author</p></article>''')
                rows = await page.evaluate(script)
            finally:
                await browser.close()
        posts = normalize_rows({'platform':'facebook','url':'https://www.facebook.com/example'},rows,100)
        self.assertEqual([(p['canonical_key'],p['text']) for p in posts],[('facebook:123','Публічний допис')])

    def test_facebook_scope_preserves_all_entries_and_skips_other_platforms(self):
        from json import loads
        catalog = loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        selected = select(catalog,facebook_only=True)
        self.assertEqual(selected['sources'],[s for s in catalog['sources'] if s['platform']=='facebook'])
        self.assertEqual(selected['platform_scope'],['facebook'])
