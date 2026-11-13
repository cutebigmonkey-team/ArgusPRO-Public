"""Regression shapes observed in cloud diagnostics, with synthetic article text."""
import io
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from article_enrichment import row_post
from browser_public import page_error
from crawl_common import CrawlState
from test_enhanced import ARTICLE,SOURCE,PARAGRAPHS
from web_pages import article_rows
from url_identity import same_site
from run import error_code


class WebsiteRepairTests(unittest.TestCase):
    def test_repeated_extraction_does_not_consume_process_global_dedup_cache(self):
        texts=[article_rows(ARTICLE,'https://news.example/story/42')[0]['text'] for _ in range(12)]
        self.assertEqual(len(set(texts)),1)
        self.assertIn(PARAGRAPHS[0],texts[-1])

    def test_www_canonical_keeps_catalog_identity(self):
        url='https://gtrkpskov.ru/television/example.html'
        html=ARTICLE.replace('</head>','<link rel="canonical" href="https://www.gtrkpskov.ru/television/example.html"></head>')
        rows=article_rows(html,url)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['url'],url)
        self.assertIn(PARAGRAPHS[0],rows[0]['text'])
        self.assertEqual(article_rows(html.replace('www.gtrkpskov.ru','other.gtrkpskov.ru'),url),[])
        self.assertFalse(same_site('https://www.gtrkpskov.ru.attacker.example/a',url))
        self.assertFalse(same_site('https://user:password@www.gtrkpskov.ru/a',url))

    def test_gorod_date_is_article_scoped_and_timezone_is_not_invented(self):
        url='https://gorod24.online/city/news/123456-example.html'
        html=ARTICLE.replace('article:published_time','unused')
        html+='<time datetime="2026-10-30T12:00:00Z">Unrelated recommendation</time>'
        html+='<div class="head-single-news-head"><h1>Rescue update</h1><div class="time"><time datetime="2026-05-12 07:56:06"></time></div></div>'
        row=article_rows(html,url)[0]
        post=row_post({**SOURCE,'url':url},row)
        self.assertIsNone(post['published_at'])
        self.assertEqual(post['published_at_raw'],'2026-05-12 07:56:06')
        self.assertTrue(post['publication_timezone_unknown'])
        self.assertIsNone(article_rows(html.replace('head-single-news-head','recommendation'),url)[0]['published_at'])
        self.assertIsNone(article_rows(html,'https://unrelated.example/news/123')[0]['published_at'])
        self.assertEqual(article_rows(html,'https://gorod24.online/'),[])

    def test_network_error_categories_preserve_cause_without_messages(self):
        for reason,expected in [(ssl.SSLCertVerificationError('private error text'),'tls_certificate_error'),
                (TimeoutError('private error text'),'timeout'),(socket.gaierror('private error text'),'dns_error')]:
            self.assertEqual(error_code(urllib.error.URLError(reason)),expected)

    def test_http_200_javascript_challenge_is_not_an_empty_news_page(self):
        html='<html><script>document.cookie="__jhash_=x";document.cookie="__jua_=x";</script></html>'
        self.assertEqual(page_error(SOURCE['url'],html),'challenge_required')
        state=CrawlState(SOURCE,5,4)
        with patch.object(state,'raw',return_value=(html.encode(),SOURCE['url']+'robots.txt')):
            with self.assertRaisesRegex(ValueError,'robots_challenge_required'):state.read(SOURCE['url'])
        self.assertTrue(state.stopped)

    def test_robots_http_status_and_cloudflare_challenge_are_distinct(self):
        for body,expected in [(b'Forbidden','robots_http_403'),
                (b'<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/test"></script>','robots_challenge_required')]:
            state=CrawlState(SOURCE,5,4)
            error=urllib.error.HTTPError(SOURCE['url'],403,'Forbidden',{},io.BytesIO(body))
            with patch.object(state,'raw',side_effect=error):
                with self.assertRaisesRegex(ValueError,expected):state.read(SOURCE['url'])
            self.assertTrue(state.stopped)

    def test_robots_html_maintenance_response_does_not_grant_crawl_permission(self):
        state=CrawlState(SOURCE,5,4)
        with patch.object(state,'raw',return_value=(b'<html><body>Maintenance</body></html>',SOURCE['url']+'robots.txt')):
            with self.assertRaisesRegex(ValueError,'robots_invalid_content'):state.read(SOURCE['url'])

    def test_disallow_captcha_path_does_not_reject_the_entire_publisher(self):
        state=CrawlState(SOURCE,5,4)
        rules=b'User-agent: *\nDisallow: /captchaKey\nDisallow: /doc-rss\n'
        with patch.object(state,'raw',return_value=(rules,SOURCE['url']+'robots.txt')):
            state.robots_allowed(SOURCE['url']+'doc/9078670')
            with self.assertRaisesRegex(ValueError,'robots_denied'):
                state.robots_allowed(SOURCE['url']+'doc-rss')


if __name__=='__main__':unittest.main()
