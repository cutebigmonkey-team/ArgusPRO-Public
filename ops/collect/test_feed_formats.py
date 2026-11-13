import json
import unittest
import urllib.error
from unittest.mock import patch
from feed_formats import parse
from run import feed_entries
from crawl_common import CrawlState
from feed_history import collect


class FeedFormats(unittest.TestCase):
    def test_json_feed_dates_edits_media_next_and_stable_url_identity(self):
        value={'version':'https://jsonfeed.org/version/1.1','next_url':'/page2',
               'items':[{'id':'a','url':'https://example.com/news?a=1&utm_source=feed',
                         'date_published':'2026-11-05T10:00:00Z','date_modified':'2026-11-05T10:05:00Z',
                         'content_text':'News','attachments':[{'url':'https://example.com/image.jpg'}]}]}
        parsed=parse(json.dumps(value).encode())
        rows=feed_entries({'id':'r','url':'https://example.com/feed','platform':'rss'},parsed.entries,10)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['published_at'],'2026-11-05T10:00:00+00:00')
        self.assertEqual(rows[0]['post_url'],'https://example.com/news?a=1')
        self.assertEqual(parsed.feed.links[0]['href'],'/page2')
        self.assertIn('https://example.com/image.jpg',rows[0]['media_urls'])

    def test_conditional_request_preserves_robots_and_304_keeps_backfill(self):
        source={'id':'r','platform':'rss','url':'https://example.com/rss','references':[{'kind':'discovered_public_feed'}]}
        previous={'url':source['url'],'http_validators':{'etag':'"v1"'},'pending':['https://example.com/older']}
        calls=[]
        def transport(url,headers,timeout,**kwargs):
            calls.append((url,headers))
            if url.endswith('/robots.txt'):return b'User-agent: *\nAllow: /',url
            if url==source['url']:
                self.assertEqual(headers.get('If-none-match'),'"v1"')
                raise urllib.error.HTTPError(url,304,'unchanged',{},None)
            return b'<rss version="2.0"><channel><item><link>https://example.com/old</link><title>Old article</title></item></channel></rss>',url
        with patch('http_pool.request',side_effect=transport),patch('crawl_common.time.sleep'),patch('run.safe_url'):
            rows,status,attempts,cursor=collect(source,previous,window_hours=0,max_pages=2)
        self.assertEqual(status,'ok');self.assertEqual(len(rows),1)
        self.assertEqual(cursor['http_validators'],previous['http_validators'])
        self.assertEqual(cursor['pending'],[])
        self.assertTrue(calls[0][0].endswith('/robots.txt'))
