import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch,AsyncMock
import urllib.error

from crawl_common import CrawlState
from enhanced import config
from web_pages import article_rows
import run
import rsshub_adapter

SOURCE={'id':'fixture','platform':'rss','url':'https://news.example/','name':'Fixture'}
PARAGRAPHS=[
    'Rescue teams reached the flooded neighbourhood early on Friday. Residents were taken to a temporary shelter and provided with drinking water and warm meals.',
    'The regional service said that three crews continued to inspect damaged buildings. Engineers checked electricity supplies before allowing families to return home.',
    'Local authorities opened an information centre at the school. Volunteers delivered blankets and emergency equipment while a separate team cleared fallen trees.',
    'Officials will publish a further update after the inspection is complete. The road remains closed to traffic and drivers have been asked to use the signed diversion.'
]
ARTICLE=('<html><head><meta property="og:type" content="article"><meta property="og:title" content="Rescue update">'
         '<meta property="article:published_time" content="2026-10-30T10:00:00Z"></head><body>'
         '<nav>MENU NOISE SUBSCRIBE LOGIN</nav><main><h1>Rescue update</h1><div id="story">'+
         ''.join('<p>'+p+'</p>' for p in PARAGRAPHS)+'</div></main><footer>FOOTER NOISE</footer></body></html>')


class EnhancedTests(unittest.TestCase):
    def test_long_native_feed_relative_permalinks_and_pagination(self):
        state=CrawlState(SOURCE,100,4)
        entries=''.join(f'<entry><title>Story {n}</title><link href="/news/{n}"/></entry>' for n in range(20))
        body=f'<feed xmlns="http://www.w3.org/2005/Atom"><link rel="next" href="/feed/page2"/>{entries}</feed>'
        self.assertEqual(state.consume(body,SOURCE['url']),['https://news.example/feed/page2'])
        self.assertEqual(len(state.items),20)

    def test_structured_media_only_article_without_date(self):
        doc={'@type':'NewsArticle','url':'/news/photo','image':'/media/photo.jpg'}
        body='<script type="application/ld+json">'+json.dumps(doc)+'</script>'
        row=article_rows(body,SOURCE['url'])[0]
        self.assertIsNone(row['published_at'])
        self.assertEqual(row['text'],'')
        self.assertEqual(row['media'],['https://news.example/media/photo.jpg'])

    def test_real_trafilatura_recovers_text_without_known_article_selector(self):
        rows=article_rows(ARTICLE,'https://news.example/story/42')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['capture_mode'],'trafilatura_article')
        self.assertIn(PARAGRAPHS[2],rows[0]['text'])
        self.assertNotIn('MENU NOISE',rows[0]['text'])
        self.assertEqual(rows[0]['published_at'],'2026-10-30T10:00:00Z')
        self.assertEqual(article_rows(ARTICLE,'https://news.example/'),[])
        self.assertIsNone(article_rows(ARTICLE.replace('article:published_time','missing'),'https://news.example/story/42')[0]['published_at'])

    def test_real_crawl4ai_markdown_is_offline_and_article_only(self):
        from importlib.metadata import version
        self.assertEqual(version('crawl4ai'),'0.9.5')
        with patch.dict(os.environ,{'ARGUS_ENHANCED_COLLECTION':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True'}):
            row=article_rows(ARTICLE,'https://news.example/story/42')[0]
        self.assertNotIn('markdown_warning',row)
        self.assertIn('crawl4ai_markdown',row['extraction_tools'])
        self.assertIn('# Rescue update',row['markdown'])
        self.assertNotIn('FOOTER NOISE',row['markdown'])

    def test_homepage_dom_and_cross_host_canonical_never_become_posts(self):
        body='<article><h1>News</h1><time datetime="2026-10-30T10:00:00Z"></time><p>Short teaser</p></article>'
        self.assertEqual(article_rows(body,'https://news.example/'),[])
        self.assertEqual(article_rows(body+'<link rel="canonical" href="https://other.example/a">','https://news.example/a'),[])

    def test_discovery_deduplicates_and_limits_same_host_links(self):
        state=CrawlState(SOURCE,5,3)
        body=''.join('<article><a href="'+u+'">A sufficiently long headline</a></article>' for u in ['/story/1','/story/1','https://other.example/a','/story/2','/story/3'])
        links=state.consume(body,SOURCE['url'])
        self.assertEqual(links,['https://news.example/story/1'])
        self.assertEqual(state.next_urls(),['https://news.example/story/2'])
        self.assertEqual(state.consume(body,SOURCE['url']),[])

    def test_feed_article_takes_priority_over_other_homepage_links(self):
        state=CrawlState(SOURCE,5,4)
        homepage='<link rel="alternate" type="application/rss+xml" href="/feed"><link rel="alternate" type="application/rss+xml" href="/other-feed">'
        self.assertEqual(state.consume(homepage,SOURCE['url']),['https://news.example/feed'])
        rss='<rss version="2.0"><channel><item><title>Rescue update</title><link>https://news.example/story/42</link><description>Short excerpt</description></item></channel></rss>'
        self.assertEqual(state.consume(rss,'https://news.example/feed'),['https://news.example/story/42'])

    def test_429_stops_queue_without_retry(self):
        state=CrawlState(SOURCE,5,4)
        with patch.object(state,'robots_allowed'),patch.object(state,'raw',side_effect=urllib.error.HTTPError(SOURCE['url'],429,'',{},None)) as fetch:
            with self.assertRaises(urllib.error.HTTPError):state.read(SOURCE['url'])
            self.assertEqual(fetch.call_count,1)
        self.assertTrue(state.stopped);self.assertEqual(state.result()['status'],'http_429')

    def test_feed_excerpt_is_enriched_with_same_key_even_at_post_limit(self):
        state=CrawlState(SOURCE,1,3)
        rss='<rss version="2.0"><channel><item><title>Rescue update</title><link>https://news.example/story/42</link><description>Short excerpt</description></item></channel></rss>'
        links=state.consume(rss,SOURCE['url'])
        self.assertEqual(links,['https://news.example/story/42'])
        key=next(iter(state.items))
        state.consume(ARTICLE,links[0])
        self.assertEqual(list(state.items),[key])
        self.assertIn(PARAGRAPHS[2],state.items[key]['text'])

    def test_robots_and_redirects_reject_forbidden_targets(self):
        state=CrawlState(SOURCE,5,4)
        with patch.object(state,'raw',return_value=(b'User-agent: *\nDisallow: /',SOURCE['url']+'robots.txt')):
            with self.assertRaisesRegex(ValueError,'robots_denied'):state.read(SOURCE['url'])
        state=CrawlState(SOURCE,5,4)
        with patch.object(state,'robots_allowed'),patch.object(state,'raw',side_effect=urllib.error.HTTPError(SOURCE['url'],302,'',{'Location':'http://127.0.0.1/admin'},None)) as fetch:
            with self.assertRaisesRegex(ValueError,'cross_origin_redirect'):state.read(SOURCE['url'])
            self.assertEqual(fetch.call_count,1)

    def test_transient_503_gets_one_retry(self):
        state=CrawlState(SOURCE,5,4)
        with patch.object(state,'robots_allowed'),patch.object(state,'raw',side_effect=[urllib.error.HTTPError(SOURCE['url'],503,'',{},None),(ARTICLE.encode(),SOURCE['url'])]) as fetch:
            state.read(SOURCE['url']);self.assertEqual(fetch.call_count,2)

    def test_rsshub_rejects_wrong_channel_and_preserves_youtube_key(self):
        source={'platform':'youtube','url':'https://www.youtube.com/channel/UC'+'a'*22}
        feed='<rss version="2.0"><channel><link>'+source['url']+'</link><item><title>Video</title><link>https://www.youtube.com/watch?v=abcdefghijk</link></item></channel></rss>'
        posts=rsshub_adapter.parse(feed,source,5)
        self.assertEqual(posts[0]['canonical_key'],'youtube:abcdefghijk')
        self.assertEqual(rsshub_adapter.parse(feed.replace('UC'+'a'*22,'UC'+'b'*22),source,5),[])
        self.assertEqual(rsshub_adapter.parse('<h1>Gateway failure</h1>',source,5),[])

    def test_enhanced_block_does_not_launch_browser_fallback(self):
        for code in ('http_403','http_429','robots_denied','robots_unavailable','challenge_required'):
            with patch.dict(os.environ,{'ARGUS_ENHANCED_COLLECTION':'1'}),patch('enhanced.collect',new=AsyncMock(return_value=([],{'tool':'crawlee','status':code,'posts':0}))),patch('browser_public.website',new=AsyncMock()) as browser:
                posts,status,_=asyncio.run(run.collect_source(object(),SOURCE,5))
                self.assertEqual(status,code);browser.assert_not_called()

    def test_both_real_engines_follow_queue_and_keep_identical_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            results=[]
            for engine in ('crawlee','scrapy'):
                output=Path(temp)/(engine+'.json')
                env={**os.environ,'CRAWLEE_STORAGE_DIR':str(Path(temp)/engine),'LITELLM_LOCAL_MODEL_COST_MAP':'True'}
                proc=subprocess.run([sys.executable,__file__,'--engine-fixture',engine,str(output)],env=env,capture_output=True,text=True,timeout=50)
                self.assertEqual(proc.returncode,0,proc.stderr[-3000:])
                result=json.loads(output.read_text());results.append(result)
                self.assertEqual(result['pages'],2,result)
                self.assertEqual(len(result['posts']),1,result)
                self.assertIn(PARAGRAPHS[1],result['posts'][0]['text'])
            self.assertEqual(results[0]['posts'][0]['canonical_key'],results[1]['posts'][0]['canonical_key'])

    def test_config_overrides_only_catalog_sources(self):
        value=config();catalog=json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        eligible={s['id'] for s in catalog['sources'] if s['platform']=='rss'}
        self.assertTrue(set(value['source_engines'])<=eligible)


def fixture(engine,output):
    from crawl_worker import crawlee_run,scrapy_run
    state=CrawlState(SOURCE,5,4)
    def raw(url):
        if url.endswith('/robots.txt'):return b'User-agent: *\nAllow: /',url
        if url==SOURCE['url']:return b'<article><a href="/story/42">Rescue operation update today</a></article>',url
        return ARTICLE.encode(),url
    with patch.object(state,'raw',side_effect=raw):
        if engine=='crawlee':asyncio.run(crawlee_run(state))
        else:scrapy_run(state)
    Path(output).write_text(json.dumps(state.result()),encoding='utf-8')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--engine-fixture':fixture(*sys.argv[2:])
    else:unittest.main()
