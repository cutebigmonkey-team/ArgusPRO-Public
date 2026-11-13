import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import source_adapters as adapters
from run import collect_source


class SourceAdapterTests(unittest.TestCase):
    reel = {'platform':'instagram', 'url':'https://www.instagram.com/reel/ABC123/'}
    threads = {'platform':'threads', 'url':'https://www.threads.net/@example/post/ABC123'}

    def test_browser_supplements_successful_profile_without_replacing_primary_rows(self):
        profile = {**self.threads, 'url': 'https://www.threads.com/@example'}
        old = {'canonical_key': 'threads:ONE', 'text': 'primary'}
        rows = [{**old, 'text': 'preview'}, {'canonical_key': 'threads:TWO'}, {'canonical_key': 'threads:THREE'}]
        env = {'ARGUS_SOURCE_ADAPTERS':'1', 'ARGUS_SOCIAL_COLLECTION':'1'}
        first = {'tool':'threads-cli', 'status':'ok', 'posts':1}
        attempt = {'tool':'threads-crawler', 'status':'ok', 'posts':3}
        with patch.dict(os.environ, env), patch.object(adapters, 'read', new_callable=AsyncMock) as read, patch('cloud_social.threads', new_callable=AsyncMock) as browser:
            read.return_value = ([old], first)
            browser.return_value = (rows, attempt)
            items, status, attempts = asyncio.run(collect_source(None, profile, 2))
        self.assertEqual(items, [old, rows[1]])
        self.assertEqual(status, 'ok')
        self.assertEqual(attempts, [first, {**attempt, 'added_posts':1}])

    def test_supplement_honors_walls_limits_permalinks_and_previous_attempts(self):
        profile = {**self.threads, 'url':'https://www.threads.com/@example'}
        row = {'canonical_key':'threads:ONE'}
        with patch('cloud_social.threads', new_callable=AsyncMock) as browser:
            for source, limit, attempts in [(profile,1,[]), (self.threads,5,[]),
                    (profile,5,[{'tool':'threads-cli','status':'http_429'}]),
                    (profile,5,[{'tool':'threads-crawler','status':'ok'}])]:
                asyncio.run(adapters.supplement_threads(source, limit, [row], attempts))
            browser.assert_not_awaited()
            browser.side_effect = TimeoutError()
            items, attempts = [row], []
            asyncio.run(adapters.supplement_threads(profile, 5, items, attempts))
            self.assertEqual(items, [row])
            self.assertEqual(attempts[0]['status'], 'timeout')

    def test_pravaha_filters_owner_id_and_duplicates(self):
        source = {'platform':'x','url':'https://x.com/alice'}
        tweet = {'id':'123','authorHandle':'Alice','text':'News', 'media':[], 'createdAt':'2026-10-31T10:00:00Z'}
        with patch.object(adapters, 'http_json', return_value={'ok':True, 'tweets':[tweet,tweet,{**tweet,'authorHandle':'bob'}]}) as call:
            item, = adapters.pravaha(source, 5)
            self.assertEqual(item['canonical_key'], 'x:123')
            call.assert_called_once_with('http://127.0.0.1:4000/api/timeline/alice')
        with patch.object(adapters, 'http_json', return_value={'ok': True, 'tweet':tweet, 'thread':[tweet]}):
            self.assertEqual(adapters.pravaha({**source, 'url':source['url']+'/status/124'}, 1), [])

    def test_one_x_reader_exception_preserves_the_batch(self):
        sources = [{'platform':'x','id':s,'url':'https://x.com/'+s} for s in ('one','two')]
        responses = [TimeoutError(), ([{'canonical_key':'x:10'}], {'tool':'pravaha','status':'ok','posts':1})]
        with patch.object(adapters, 'read', new_callable=AsyncMock, side_effect=responses):
            prepared, stopped = asyncio.run(adapters.prime_x_sources(sources,30,0))
        self.assertFalse(stopped)
        self.assertEqual(prepared['one'][1]['status'],'timeout')
        self.assertEqual(prepared['two'][0][0]['canonical_key'],'x:10')

    def test_x_first_pass_reads_later_profile_after_empty_and_stops_at_429(self):
        sources = [{'platform':'x', 'id':k, 'url':'https://x.com/'+k} for k in ('one','two','three','four')]
        found = {'canonical_key':'x:10'}
        responses = [([], {'status':'no_public_posts'}), ([found], {'status':'ok'}), ([], {'status':'http_429'})]
        with patch.object(adapters, 'read', new_callable=AsyncMock, side_effect=responses) as read:
            prepared, stopped = asyncio.run(adapters.prime_x_sources(sources, 30, 0))
        self.assertTrue(stopped)
        self.assertEqual(list(prepared), ['one','two','three'])
        self.assertEqual(read.await_count, 3)
        legacy = AsyncMock(return_value=([], 'http_429', [{'tool':'public_embed','status':'http_429'}]))
        with patch.object(adapters, 'read', new_callable=AsyncMock) as reread:
            empty = asyncio.run(adapters.collect(None, sources[0], 30, False, legacy, None, prepared['one']))
            success = asyncio.run(adapters.collect(None, sources[1], 30, False, legacy, None, prepared['two']))
        self.assertEqual(empty[1], 'http_429')
        self.assertEqual(success[0], [found])
        self.assertEqual(legacy.await_count, 1)
        reread.assert_not_awaited()

    def test_og_metadata_is_bound_to_exact_permalink(self):
        html = '<meta property="og:url" content="%s"><meta property="og:description" content="Caption">'
        for url, count in [(self.reel['url'], 1), (self.reel['url'].replace('ABC123','ABC124'), 0),
                           ('https://example.org/reel/ABC123/', 0)]:
            with patch('source_adapters.PublicHTTP.request', return_value=html % url):
                rows = adapters.social_fetch(self.reel)
                self.assertEqual(len(rows), count)
                if rows:
                    self.assertTrue(rows[0]['text_may_be_truncated'])

    def test_reels_requires_matching_nested_id_and_keeps_comments_separate(self):
        raw = {'success':True, 'url':self.reel['url'], 'shortcode':'ABC123', 'captionText':'My reel',
               'videoUrlBest':'https://cdn.example/video', 'takenAt':1756742460,
               'commentsExtracted':[{'id':'123','text':'A comment'}]}
        data = [raw]
        item, = adapters.single_reel_rows(self.reel, data)
        self.assertEqual(item['text'], 'My reel')
        self.assertEqual(item['instagram_comments'][0]['text'], 'A comment')
        self.assertIsNotNone(item['published_at'])
        raw['shortcode'] = 'ABC124'
        self.assertEqual(adapters.single_reel_rows(self.reel, data), [])

    def test_actor_routes_are_bounded(self):
        raw = {'url':self.threads['url'], 'text':'Hello', 'author':{'username':'example'}, 'likeCount':2}
        with patch.object(adapters, 'http_json', return_value=[raw, {**raw, 'author':{'username':'other'}}]) as call:
            row, = adapters.apify('logiover-threads', self.threads, 100, 'test')
            self.assertEqual(row['engagement']['like_count'], 2)
            self.assertEqual(call.call_args.kwargs['body']['maxResults'], 5)
            self.assertFalse(call.call_args.kwargs['body']['includeReplies'])
        with patch.object(adapters,'require_cloud'), patch.object(adapters,'http_json') as http:
            _, attempt = asyncio.run(adapters.read('socialdataapi-reels', self.reel, 1))
            self.assertEqual(attempt['status'], 'not_configured')
            http.assert_not_called()

    def test_primary_wall_stops_and_free_fallback_precedes_actor(self):
        source = {**self.threads, 'url':'https://www.threads.net/@example'}
        env = {'ARGUS_SOURCE_ADAPTERS':'1','ARGUS_LOGIOVER_ENABLED':'true'}
        for status in ('http_429','no_public_posts'):
            with patch.dict(os.environ,env), patch.object(adapters,'read',new_callable=AsyncMock) as read, patch('run.collect_existing',new_callable=AsyncMock) as legacy:
                read.side_effect = [([],{'tool':'threads-cli','status':status}), ([],{'tool':'logiover-threads','status':'ok'})]
                legacy.return_value = [],'no_public_posts',[{'tool':'legacy','status':'no_public_posts'}]
                _, _, attempts = asyncio.run(collect_source(None, source, 3, actor_token='test'))
                expected = ['threads-cli'] if status == 'http_429' else ['threads-cli','legacy','logiover-threads']
                self.assertEqual([a['tool'] for a in attempts],expected)

    def test_media_only_for_verified_item_and_no_temporary_tunnel(self):
        item = {'platform':'instagram', 'post_url':self.reel['url'], 'canonical_key':'instagram:ABC123','media_urls':[]}
        attempts = []
        with patch.object(adapters, 'http_json', return_value={'status':'tunnel','url':'http://127.0.0.1:9000/tunnel?id=1'}) as http:
            asyncio.run(adapters.enrich_media([{**item,'canonical_key':'instagram:other'},item],attempts))
            self.assertEqual(attempts[0]['status'],'temporary_tunnel')
            self.assertEqual(item['media_urls'],[])
            http.assert_called_once()
        with patch.object(adapters,'http_json',return_value={'status':'picker','picker':[{'url':'https://cdn.example/a'}]}), patch('run.safe_url'):
            asyncio.run(adapters.enrich_media([item],attempts))
            self.assertEqual(item['media_urls'],['https://cdn.example/a'])

    def test_profile_reel_recovery_respects_separate_switch(self):
        profile = {'platform':'instagram', 'url':'https://www.instagram.com/example/'}
        for flag in ('false', 'true'):
            item = {'canonical_key':'instagram:ABC123','post_url':self.reel['url'],'text':'Original','media_urls':[]}
            rows = [{'canonical_key':item['canonical_key'],'text':'Caption','published_at':None,'media_urls':['https://cdn.example/v']}]
            attempts = []
            with patch.dict(os.environ, {'ARGUS_SOCIALDATAAPI_REELS_ENABLED':flag}), patch.object(adapters, 'read', new_callable=AsyncMock) as read:
                read.side_effect = [([], {'status':'no_public_posts'}), (rows, {'status':'ok'})]
                asyncio.run(adapters.enrich_detail(profile, [item], attempts, 'test'))
                self.assertEqual(item['text'], 'Original')
                self.assertEqual(bool(item['media_urls']), flag == 'true')
                if flag == 'true':
                    self.assertEqual(read.await_args.args[0], 'socialdataapi-reels')
