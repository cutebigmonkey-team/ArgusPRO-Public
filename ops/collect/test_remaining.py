import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import remaining_social as social
from remaining_http import PublicHTTP, StopCollection
from remaining_worker import evidence_rows, skraper, socialfetch, granary, facebook_permalink
from remaining_worker import fbgql as fbgql_worker

IG = {'data':{'user':{'id':'7','username':'example','is_private':False,
      'edge_owner_to_timeline_media':{'edges':[{'node':{'id':'42','shortcode':'AbC',
      '__typename':'GraphImage','is_video':False,'dimensions':{'width':640,'height':480},
      'owner':{'id':'7','username':'example'},'taken_at_timestamp':1793318400,
      'display_url':'https://cdn.example/image.jpg',
      'edge_media_to_caption':{'edges':[{'node':{'text':'Український допис'}}]}}}]}}}}
HTML = {
    'facebook':'''<article data-ft='{"top_level_post_id":"42"}'><header><a href="/example">Example</a></header><p>Український допис</p><a href="/example/posts/42">Date</a></article>''',
    'vk':'''<div class="post"><div class="PostHeaderTitle"><a href="/example">Example</a></div><a class="PostHeaderSubtitle__link" href="/wall-7_42">Date</a><div class="wall_post_text">Український допис</div></div>''',
    'ok':'''<div data-feed-id="feed-is-not-post-id"><span class="feed_author"><a href="/example">Example</a></span><div class="media-text_cnt">Український допис</div><a href="/example/topic/42">Date</a></div>'''
}


def source(platform):
    return {'id':'fixture','platform':platform,'url':'https://'+{'instagram':'www.instagram.com','facebook':'www.facebook.com','vk':'vk.com','ok':'ok.ru'}[platform]+'/example'}


class IdentityTests(unittest.TestCase):
    def test_reject_foreign_author_profile_url_and_foreign_host(self):
        rows = [{'url':'https://www.instagram.com/p/AbC/','source_owner':'foreign','text':'x'},
                {'url':'https://www.instagram.com/example','source_owner':'example','text':'profile'},
                {'url':'https://evil.test/p/AbC/','source_owner':'example','text':'x'}]
        self.assertEqual(social.normalize(source('instagram'), rows, 5, 'skraper'), [])

    def test_direct_post_rejects_other_post_and_deduplicates(self):
        s = {**source('instagram'), 'url':'https://www.instagram.com/p/AbC/'}
        row = {'url':s['url'],'text':'post','published':1793318400}
        got = social.normalize(s, [row,row,{**row,'url':'https://www.instagram.com/p/XYZ/'}], 5, 'socialfetch')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['canonical_key'], 'instagram:AbC')
        self.assertTrue(got[0]['published_at'].startswith('2026-'))

    def test_skraper_ok_feed_id_is_never_used_as_post_id(self):
        rows = evidence_rows(source('ok'), [{'id':'feed-is-not-post-id','text':'Post'}], HTML['ok'])
        result = social.normalize(source('ok'), rows, 5, 'skraper')
        self.assertEqual(result[0]['canonical_key'], 'ok:42')
        self.assertEqual(evidence_rows(source('ok'), [{'id':'42','text':'wrong'}], HTML['ok']), [])

    def test_repost_link_in_text_is_not_author_evidence(self):
        body = HTML['vk'].replace('href="/example">Example', 'href="/foreign">Foreign') + '<a href="/example">Example</a>'
        self.assertEqual(evidence_rows(source('vk'), [{'id':'wall-7_42','text':'x'}], body), [])

    def test_ok_numeric_group_matches_vanity_permalink_using_card_context(self):
        import html
        s = {**source('ok'),'url':'https://ok.ru/group/7'}
        context = {'type':'FEED','data':{'feedLocation':'GROUP','ownerContentId':'7',
                   'feedId':'opaque-feed-id','mediaTopicId':'42','isFriendsOnly':False}}
        def body(value):
            return '<div data-feed-id="opaque-feed-id" data-seen-params="'+html.escape(json.dumps(value),quote=True)+'"><div class="feed-header"><a href="/vanity">Group</a></div><a href="/vanity/topic/42?tracking=x"><img src="//cdn.example/photo.jpg"></a></div>'
        rows = [{'id':'opaque-feed-id','text':'Post'}]
        verified = evidence_rows(s,rows,body(context))
        got = social.normalize(s,verified,5,'skraper')
        self.assertEqual(got[0]['canonical_key'],'ok:42')
        self.assertEqual(got[0]['post_url'],'https://ok.ru/vanity/topic/42')
        self.assertEqual(got[0]['media_urls'],['https://cdn.example/photo.jpg'])
        for field, value in [('ownerContentId','8'),('feedId','wrong'),('mediaTopicId','43'),('isFriendsOnly',True)]:
            with self.subTest(field=field):
                altered = {**context,'data':{**context['data'],field:value}}
                self.assertEqual(evidence_rows(s,rows,body(altered)),[])

    def test_ig_owner_is_bound_to_profile_and_media_owner(self):
        rows = [{'id':'42','text':'post'}]
        body = json.dumps(IG)
        self.assertEqual(len(evidence_rows(source('instagram'), rows, body)), 1)
        self.assertEqual(evidence_rows(source('instagram'), rows, body.replace('"id": "7"','"id": "8"',1)), [])
        self.assertEqual(evidence_rows(source('instagram'), rows, body.replace('example','foreign')), [])

    def test_numeric_facebook_permalink_needs_matching_proven_ids(self):
        self.assertEqual(facebook_permalink('https://www.facebook.com/42','42','7'),
                         'https://www.facebook.com/permalink.php?story_fbid=42&id=7')
        self.assertEqual(facebook_permalink('https://www.facebook.com/42','43','7'), 'https://www.facebook.com/42')
        self.assertEqual(facebook_permalink('https://evil.test/42','42','7'), 'https://evil.test/42')

    def test_cloud_guard(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'false'},clear=True):
            with self.assertRaises(RuntimeError):
                asyncio.run(social.collect(source('vk'), 5))

    def test_explicit_hostless_linux_is_allowed_but_unmarked_local_is_not(self):
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','linux'):
            social.require_cloud()
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','win32'):
            with self.assertRaises(RuntimeError):social.require_cloud()

    def test_stop_does_not_fall_through_to_other_tools(self):
        with patch.object(social, 'require_cloud'), patch.object(social, 'adapter', new_callable=AsyncMock) as call:
            call.return_value = [], social.diagnostic('fbgql','http_429',[])
            _, attempts = asyncio.run(social.collect(source('facebook'),5))
            self.assertEqual(len(attempts),1)
            call.assert_awaited_once()

    def test_vk_no_token_is_explicit_and_never_launches_process(self):
        with patch.object(social,'require_cloud'), patch.dict(os.environ,{'ARGUS_VK_TOKEN':''}), patch.object(social,'process',new_callable=AsyncMock) as call:
            _, attempt = asyncio.run(social.adapter(source('vk'),5,'vkrss'))
            self.assertEqual(attempt['status'],'requires_vk_token')
            call.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_restrictions_escape_upstream_exception_retry_handlers(self):
        self.assertFalse(issubclass(StopCollection, Exception))

    def test_off_platform_request_rejected_before_network(self):
        with patch('requests.Session') as session:
            with self.assertRaises(StopCollection) as exc:
                PublicHTTP('facebook').request('https://example.com/')
            self.assertEqual(exc.exception.status, 'unexpected_destination')
            session.assert_not_called()

    def test_429_is_not_retried_and_secret_headers_removed(self):
        with patch('run.safe_url'), patch('requests.Session') as session:
            client = session.return_value.__enter__.return_value
            response = client.request.return_value.__enter__.return_value
            response.status_code = 429
            with self.assertRaises(StopCollection) as exc:
                PublicHTTP('facebook').request('https://www.facebook.com/example', headers={'Cookie':'secret','Authorization':'secret'})
            self.assertEqual(exc.exception.status, 'http_429')
            self.assertEqual(client.request.call_count, 1)
            self.assertNotIn('Cookie', client.request.call_args.kwargs['headers'])

    def test_login_redirect_never_fetches_login_form(self):
        with patch('run.safe_url'), patch('requests.Session') as session:
            client = session.return_value.__enter__.return_value
            response = client.request.return_value.__enter__.return_value
            response.status_code = 302
            response.headers = {'Location':'/login/'}
            with self.assertRaises(StopCollection) as exc:
                PublicHTTP('facebook').request('https://www.facebook.com/example')
            self.assertEqual(exc.exception.status, 'login_required')
            self.assertEqual(client.request.call_count, 1)


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.tools = social.tool_root()

    def test_actual_skraper_parsers_four_platforms(self):
        if not (self.tools/'skraper/cli/target/cli.jar').exists():
            self.skipTest('Pinned JVM parser is built on the cloud runner')
        for platform in ('instagram','facebook','vk','ok'):
            body = json.dumps(IG) if platform=='instagram' else HTML[platform]
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                http = type('OfflineHTTP', (), {'request':lambda *args, **kwargs:body})()
                rows = skraper(source(platform), self.tools, http, Path(directory))
                got = social.normalize(source(platform), rows, 5, 'skraper')
                self.assertEqual(len(got), 1)
                self.assertEqual(got[0]['text'], 'Український допис')

    def test_actual_socialfetch_feed_parser(self):
        if not (self.tools/'socialfetch/socialfetch').exists():
            self.skipTest('Pinned source installed in cloud CI')
        body = json.dumps({'items':[{'pk':'42','code':'AbC','user':{'username':'example'},'caption':{'text':'Допис'}}]})
        http = type('OfflineHTTP', (), {'request':lambda *args, **kwargs:body})()
        rows = socialfetch(source('instagram'), self.tools, http)
        self.assertEqual(social.normalize(source('instagram'),rows,5,'socialfetch')[0]['text'], 'Допис')

    def test_actual_fbgql_discovery_and_feed_parser(self):
        try:
            import fbgql
        except ImportError:
            self.skipTest('Pinned fbgql installed in isolated cloud environment')
        import runpy
        fixture = runpy.run_path(str(self.tools/'fbgql/tests/fixtures.py'))['TIMELINE_PAGE']
        fixture = fixture.replace('https://www.facebook.com/999', 'https://www.facebook.com/example/posts/999')
        class OfflineHTTP:
            def request(self, url, method='GET', *args, **kwargs):
                return fixture if method == 'POST' else '<meta property="al:android:url" content="fb://page/?id=123456789" />'
        rows = fbgql_worker(source('facebook'), OfflineHTTP())
        got = social.normalize(source('facebook'), rows, 5, 'fbgql')
        self.assertEqual(got[0]['canonical_key'], 'facebook:999')
        self.assertEqual(got[0]['text'], 'a post')

    def test_actual_granary_instagram_converter(self):
        try:
            from granary import instagram as upstream_instagram
        except ImportError:
            self.skipTest('Pinned granary is installed in its isolated cloud environment')
        http = type('OfflineHTTP', (), {'request':lambda *args, **kwargs:json.dumps(IG)})()
        rows = granary(source('instagram'), http)
        self.assertEqual(social.normalize(source('instagram'),rows,5,'granary')[0]['canonical_key'], 'instagram:AbC')

    def test_actual_vkrss_generator(self):
        if not (self.tools/'vkrss/Vk2rss.php').exists():
            self.skipTest('Pinned PHP library installed in cloud CI')
        import subprocess
        import feedparser
        result = subprocess.run(['php',str(Path(__file__).with_name('vkrss_smoke.php')),str(self.tools/'vkrss')],
                                capture_output=True, text=True, encoding='utf-8', timeout=8, check=True)
        feed = feedparser.parse(result.stdout)
        self.assertEqual(feed.entries[0].link, 'https://vk.ru/wall-7_42')
        self.assertIn('Український допис', feed.entries[0].summary)
        self.assertNotIn('offline-fixture', result.stdout)

    def test_actual_granary_facebook_permalink_and_attachments(self):
        try:
            from granary import facebook as upstream_facebook
        except ImportError:
            self.skipTest('Pinned granary installed in isolated cloud environment')
        body = '''<article id="u_0_1"><header><strong><a href="/example">Example</a></strong></header>
        <div><div><p>Український допис</p></div></div><a href="/photo.php?fbid=8"><img src="https://cdn.example/photo.jpg"></a>
        <footer><a href="/story.php?story_fbid=42&amp;id=7">Post</a>Public</footer></article>'''
        http = type('OfflineHTTP', (), {'request':lambda *args, **kwargs:body})()
        rows = granary(source('facebook'), http)
        got = social.normalize(source('facebook'), rows, 5, 'granary')
        self.assertEqual(got[0]['canonical_key'], 'facebook:42')
        self.assertIn('Український допис',got[0]['text'])
        self.assertIn('https://cdn.example/photo.jpg',got[0]['media_urls'])


if __name__ == '__main__':
    unittest.main()
