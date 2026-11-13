import asyncio
import json
import unittest
from unittest.mock import patch, AsyncMock
import run
from public_embeds import parse_tiktok,parse_x
from browser_public import normalize_rows
from web_pages import article_rows
from media_metadata import normalize


class PublicTests(unittest.TestCase):
    def test_tiktok_embed_uses_only_requested_profile_and_deduplicates(self):
        s={'platform':'tiktok','url':'https://www.tiktok.com/@example'}
        row={'id':'123456789123','desc':'Real video','createTime':1793275200}
        payload={'source':{'data':{'/embed/@example':{'videoList':[row,row,{**row,'id':'123456789999','author':'someone_else'}]},'/embed/@other':{'videoList':[row]}}}}
        html='<script id="__FRONTITY_CONNECT_STATE__">'+json.dumps(payload)+'</script>'
        items=parse_tiktok(html,s,5)
        self.assertEqual(len(items),1);self.assertEqual(items[0]['canonical_key'],'tiktok:123456789123')
        self.assertEqual(parse_tiktok('<h1>Profile bio</h1>',s,5),[])

    def test_x_embed_filters_recommendations_and_missing_ids(self):
        source={'platform':'x','url':'https://x.com/example'}
        tweet={'id_str':'1234','user':{'screen_name':'Example'},'full_text':'Actual post','created_at':'Wed Oct 28 12:00:00 +0000 2026'}
        entries=[{'content':{'tweet':tweet}},{'content':{'tweet':{**tweet,'id_str':'5678','user':{'screen_name':'other'}}}}]
        html='<script id="__NEXT_DATA__">'+json.dumps({'props':{'pageProps':{'timeline':{'entries':entries}}}})+'</script>'
        items=parse_x(html,source,5)
        self.assertEqual(len(items),1);self.assertEqual(items[0]['published_at'],'2026-10-28T12:00:00+00:00')

    def test_facebook_accepts_own_reels_and_rejects_comments_or_other_pages(self):
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        rows=[{'url':'https://www.facebook.com/example/reel/123','text':'Actual video'},
              {'url':'https://www.facebook.com/example/posts/456?comment_id=8','text':'Comment'},
              {'url':'https://www.facebook.com/other/posts/789','text':'Other page'}]
        items=normalize_rows(source,rows,5)
        self.assertEqual(len(items),1);self.assertEqual(items[0]['canonical_key'],'facebook:123')
        source['url']='https://www.facebook.com/profile.php?id=42'
        rows=[{'url':'https://www.facebook.com/permalink.php?story_fbid=123&id=42','text':'Actual post'}]
        self.assertEqual(len(normalize_rows(source,rows,5)),1)

    def test_article_metadata_requires_article_identity_but_date_is_optional(self):
        body='<meta property="og:type" content="article"><meta property="og:title" content="News"><meta property="article:published_time" content="2026-10-29T10:00:00Z"><article><p>Complete paragraph</p></article>'
        self.assertEqual(article_rows(body,'https://news.example/news/1')[0]['text'],'News\n\nComplete paragraph')
        self.assertIsNone(article_rows(body.replace('article:published_time','other'),'https://news.example/news/1')[0]['published_at'])
        self.assertEqual(article_rows(body,'https://news.example/'),[])
        self.assertEqual(article_rows(body+'<link rel="canonical" href="https://evil.example/post">','https://news.example/news/1'),[])

    def test_embed_rate_limit_stops_fallback_and_success_skips_browser(self):
        source={'platform':'tiktok','url':'https://www.tiktok.com/@example'}
        browser=AsyncMock()
        with patch('public_embeds.collect',side_effect=ValueError('http_429')),patch('run.social',browser):
            items,status,attempts=asyncio.run(run.collect_source(None,source,3,True))
        browser.assert_not_called();self.assertEqual(status,'http_429');self.assertEqual(len(attempts),1)
        with patch('public_embeds.collect',return_value=[{'canonical_key':'tiktok:12345678'}]),patch('run.social',browser):
            items,status,attempts=asyncio.run(run.collect_source(None,source,3,True))
        browser.assert_not_called();self.assertEqual(status,'ok')

    def test_publisher_embed_matches_cite_video_id_and_byline(self):
        from public_embeds import publisher_tiktok_rows
        source={'id':'sample','platform':'tiktok','url':'https://www.tiktok.com/@example','origin':'general'}
        url='https://news.example/article'
        card=('<blockquote class="tiktok-embed" cite="https://www.tiktok.com/@example/video/123456789" '
              'data-video-id="123456789"><section><a href="https://www.tiktok.com/@example?refer=embed">@example</a>'
              '<p>Original caption</p><a href="https://www.tiktok.com/music/song-42">Music title</a></section></blockquote>')
        rows=publisher_tiktok_rows(source,'<time datetime="2026-11-01T12:00:00Z"></time>'+card+card,url)
        self.assertEqual([(r['canonical_key'],r['text'],r['published_at'],r['publisher_embed_url']) for r in rows],
                         [('tiktok:123456789','Original caption',None,url)])
        for invalid in (card.replace('123456789"','123456780"',1),card.replace('@example?','@foreign?'),
                        card.replace('@example/video','@foreign/video'),card.replace('Original caption','')):
            self.assertEqual(publisher_tiktok_rows(source,invalid,url),[])

    def test_publisher_embeds_do_not_replace_native_caption_on_rereads(self):
        from record_quality import merge_observations
        native={'capture_mode':'public_creator_embed','text':'Original native caption','observed_at':'2026-10-01T00:00:00Z',
                'text_may_be_truncated':True}
        embed={**native,'capture_mode':'publisher_tiktok_embed','text':'Cached caption','observed_at':'2026-11-01T00:00:00Z'}
        row=merge_observations(native,embed)
        row=merge_observations(row,{**embed,'observed_at':'2026-11-02T00:00:00Z'})
        self.assertEqual((row['capture_mode'],row['text']),(native['capture_mode'],native['text']))

    def test_broken_feed_discovery_does_not_hide_working_second_feed(self):
        source={'platform':'rss','url':'https://example.com'}
        html='<link rel="alternate" type="application/rss+xml" href="/broken"><link rel="alternate" type="application/rss+xml" href="/feed">'
        rss='<rss version="2.0"><channel><item><title>News</title><link>https://example.com/news/1</link></item></channel></rss>'
        with patch('run.fetch',side_effect=[(html,source['url']),ValueError('http_404'),(rss,source['url']+'/feed')]):
            self.assertEqual(len(run.feeds(source,3)),1)

    def test_tiktok_media_rejects_cross_profile_urls_and_keeps_embed_identity(self):
        source={'platform':'tiktok','url':'https://www.tiktok.com/@example'}
        row={'id':'123456789123','title':'Actual video','webpage_url':'https://www.tiktok.com/@example/video/123456789123'}
        self.assertEqual(normalize(source,row,3)[0]['canonical_key'],'tiktok:123456789123')
        self.assertEqual(normalize(source,{**row,'webpage_url':row['webpage_url'].replace('@example','@other')},3),[])
        self.assertFalse(run.source_valid({'platform':'instagram','url':'https://www.instagram.com/accounts/login'}))


if __name__=='__main__':unittest.main()
