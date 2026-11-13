import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import run as collector

class CollectorTests(unittest.TestCase):
    def test_private_destinations_and_cross_platform_sources_rejected(self):
        with patch('socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
            collector.public_host.cache_clear()
            with self.assertRaises(ValueError): collector.safe_url('https://private.test/')
        self.assertFalse(collector.source_valid({'platform':'telegram','url':'https://example.com/abc'}))
        self.assertFalse(collector.url_ok('https://user:password@example.com/'))
        self.assertFalse(collector.url_ok('file:///etc/passwd'))

    def test_telegram_requires_post_identity_not_channel_description(self):
        source={'platform':'telegram','url':'https://t.me/example','name':'Example'}
        body=b'''<div class="tgme_channel_info">Not a post</div>
        <div class="tgme_widget_message" data-post="example/42"><div class="tgme_widget_message_text">Real &amp; text</div><time datetime="2026-01-02T10:00:00+00:00"></time></div>
        <div class="tgme_widget_message" data-post="other/3"><div class="tgme_widget_message_text">Other channel</div></div>'''
        with patch.object(collector,'fetch',return_value=(body,source['url'])):
            posts=collector.telegram(source,10)
        self.assertEqual(len(posts),1);self.assertEqual(posts[0]['canonical_key'],'telegram:example/42')
        self.assertEqual(posts[0]['text'],'Real & text')

    def test_telegram_poll_is_authored_content_but_app_notice_is_not(self):
        source = {'platform':'telegram','url':'https://t.me/example'}
        body = '''<div class="tgme_widget_message" data-post="example/41">
          <div class="tgme_widget_message_poll_question">Question?</div>
          <div class="tgme_widget_message_poll_option_text">Choice</div></div>
          <div class="tgme_widget_message" data-post="example/42">VIEW IN TELEGRAM</div>'''
        rows = collector.telegram_html(source, 100, body)
        self.assertEqual([(p['canonical_key'],p['text']) for p in rows], [('telegram:example/41','Question?\nChoice')])

    def test_wall_posts_keep_separate_ids_media_owner_and_epoch(self):
        from browser_public import normalize_rows
        source = {'platform':'vk','url':'https://vk.com/club7'}
        rows = [dict(url=f'/wall{owner}_{post}',text='Same topic',publishedAt='1793700000000')
                for owner,post in [(-7,1),(-7,2),(-8,3)]]
        posts = normalize_rows(source, rows, 1)
        self.assertEqual([p['canonical_key'] for p in posts], ['vk:-7_1','vk:-7_2'])
        self.assertTrue(all(p['published_at'].startswith('2026-') for p in posts))
        source = {'platform':'ok','url':'https://ok.ru/group/7'}
        card = dict(url='/alias/topic/42',groupId='7',images=['https://cdn.example/photo.jpg'])
        self.assertEqual(normalize_rows(source,[card],1)[0]['canonical_key'],'ok:42')
        self.assertEqual(normalize_rows(source,[{**card,'privateCard':True},{**card,'groupId':'8'}],1),[])

    def test_feed_html_is_not_a_post_and_dates_are_not_invented(self):
        source={'platform':'rss','url':'https://example.com/feed'}
        self.assertEqual(collector.feed_posts(source,b'<html>Log in</html>',10),[])
        body=b'<rss version="2.0"><channel><title>Feed</title><item><title>One</title><link>https://example.com/post/1</link><description>&lt;p&gt;Text&lt;/p&gt;</description></item></channel></rss>'
        one=collector.feed_posts(source,body,10)[0]
        self.assertIsNone(one['published_at']);self.assertEqual(one['text'],'One\n\nText')
        self.assertEqual(one['canonical_key'],collector.feed_posts(source,body,10)[0]['canonical_key'])

    def test_error_reporting_never_copies_remote_error_text(self):
        self.assertEqual(collector.error_code(ValueError('token=private_secret')),'collection_failed')
        self.assertEqual(collector.error_code(ValueError('login_required')),'login_required')
        self.assertFalse(collector.url_ok('https://example.com:bad/'))

    def test_facebook_permalink_preserves_identity_but_not_tracking(self):
        self.assertEqual(collector.social_url('https://www.facebook.com/permalink.php?story_fbid=42&id=7&__cft__=tracking'),
                         'https://www.facebook.com/permalink.php?story_fbid=42&id=7')

if __name__=='__main__': unittest.main()
