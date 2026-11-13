import asyncio
import datetime as dt
import unittest
from unittest.mock import AsyncMock, patch

import run
from cloud_social import worker_env
from facebook_feed import story_post
from record_quality import merge_observations
from source_health import preferred, reader_history, update, instant


class ReliabilityTests(unittest.TestCase):
    def test_updated_only_feed_and_media_previews(self):
        feed=b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
        <entry><link href="https://news.test/a?utm_source=x&amp;id=3"/><title>Story</title>
        <updated>2026-01-05T10:00:00Z</updated><media:thumbnail url="https://news.test/image"/></entry>
        <entry><link href="https://news.test/a?id=3"/><title>Story</title></entry></feed>'''
        row,=run.feed_posts({'platform':'rss','url':'https://news.test/'},feed,5)
        self.assertIsNone(row['published_at'])
        self.assertEqual(row['post_url'],'https://news.test/a?id=3')
        self.assertEqual(row['media_urls'],['https://news.test/image'])

        self.assertIsNone(run.feed_date({'published':'2026-11-01T12:00:00'}))
        self.assertIsNone(run.feed_date({'updated':'2026-11-01T12:00:00Z'}))

    def test_feed_next_uses_same_site_existing_crawl_budget(self):
        from crawl_common import CrawlState
        src={'platform':'rss','url':'https://news.test/feed'}
        state=CrawlState(src,5,2)
        body=b'''<feed xmlns="http://www.w3.org/2005/Atom">
            <link rel="next" href="https://news.test/feed?page=2"/>
            <entry><link href="https://news.test/art"/><title>Article</title></entry></feed>'''
        self.assertEqual(state.consume(body,src['url']),['https://news.test/feed?page=2'])
        self.assertEqual(state.consume(body,src['url']+'?page=2'),[])
        self.assertEqual(len(state.items),1)

    def test_public_story_timestamp_and_comments_belong_to_same_post(self):
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        content={'post_id':'42','wwwURL':source['url']+'/posts/42','creation_time':'1735689600','message':{'text':'Body'}}
        story={'comet_sections':{'content':{'story':content}},'feedback':{'comments':{'edges':[
            {'node':{'id':'c1','body':{'text':'Visible reply'}}}]}}}
        row=story_post(source,story)
        self.assertEqual(row['published_at'],'2025-01-01T00:00:00+00:00')
        self.assertEqual(row['public_comments'],[{'id':'c1','text':'Visible reply'}])
        content['post_id']='43'
        self.assertIsNone(story_post(source,story))

    def test_partial_facebook_feed_is_enriched_and_keeps_both_pages(self):
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        first=run.post(source,'facebook:1',source['url']+'/posts/1','Teaser')
        first['text_may_be_truncated']=True
        full={**first,'text':'Complete post','published_at':'2026-01-01T00:00:00+00:00','text_may_be_truncated':False}
        second={**full,'canonical_key':'facebook:2','post_url':source['url']+'/posts/2'}
        with patch.dict('os.environ',{'ARGUS_REMAINING_COLLECTION':'1'}), patch('remaining_social.adapter',new=AsyncMock(
                return_value=([first],{'tool':'fbgql','status':'ok','posts':1}))), patch('facebook_feed.collect',new=AsyncMock(
                return_value=([full,second],'ok',[{'tool':'facebook_public_feed','status':'ok','posts':2}]))):
            rows,status,_=asyncio.run(run.collect_source(object(),source,5))
        self.assertEqual((status,len(rows),rows[0]['text']),('ok',2,'Complete post'))

    def test_complete_single_facebook_card_does_not_end_timeline_scan(self):
        source={'platform':'facebook','url':'https://www.facebook.com/example','origin':'dsns'}
        row=run.post(source,'facebook:1',source['url']+'/posts/1','Full post','2026-01-01T00:00:00Z')
        row['text_may_be_truncated']=False
        second={**row,'canonical_key':'facebook:2','post_url':source['url']+'/posts/2'}
        with patch.dict('os.environ',{'ARGUS_REMAINING_COLLECTION':'1'}), patch('remaining_social.adapter',new=AsyncMock(
                return_value=([row],{'tool':'fbgql','status':'ok','posts':1}))), patch('facebook_feed.collect',new=AsyncMock(
                return_value=([row,second],'ok',[{'tool':'facebook_public_feed','status':'ok','posts':2}]))):
            rows,status,_=asyncio.run(run.collect_source(object(),source,2))
        self.assertEqual([r['canonical_key'] for r in rows],['facebook:1','facebook:2'])
        self.assertTrue(all(r['origin']=='dsns' for r in rows))

    def test_monitor_separates_parse_failures_quiet_and_deferred_sources(self):
        from source_health import platform_status
        self.assertEqual(platform_status(1,['unverified_markup'],0),'failed')
        self.assertEqual(platform_status(1,['no_public_posts'],0),'no_posts')
        self.assertEqual(platform_status(1,['not_due'],0),'partial')
        self.assertEqual(platform_status(2,['ok','http_429'],3),'partial')

    def test_recent_publication_remains_due_after_a_quiet_poll(self):
        record = dict(platform='facebook', url='https://example.org/news', checked_at='2026-11-03T10:20:00Z',
                      new_keys=[], status='no_new_posts', seconds=1, attempts=[])
        prior = dict(url=record['url'], quiet_checks=4, last_post_published_at='2026-11-03T10:01:00Z')
        self.assertEqual(instant(update(prior, record)['next_check_at']), instant('2026-11-03T10:21:00Z'))
        later = {**record, 'retry_after':'2026-11-03T12:00:00Z'}
        self.assertEqual(instant(update(prior, later)['next_check_at']), instant(later['retry_after']))

    def test_verified_undated_wall_keeps_the_normal_platform_interval(self):
        record=dict(platform='ok',url='https://ok.ru/group/7',checked_at='2026-11-03T12:00:00Z',
                    new_keys=[],status='no_new_posts',attempts=[],seconds=1,delivery={'saved_ids':10})
        prior={'url':record['url'],'quiet_checks':4}
        self.assertEqual(instant(update(prior,record)['next_check_at']),instant('2026-11-03T12:10:00Z'))
        wall=update(prior,{**record,'status':'login_required'})
        self.assertGreaterEqual(instant(wall['next_check_at']),instant('2026-11-04T12:00:00Z'))

    def test_replays_and_stale_results_do_not_reset_health(self):
        record={'url':'https://t.me/example','checked_at':'2026-11-01T00:00:00Z','new_keys':[],'status':'ok',
                'seconds':4,'attempts':[{'tool':'a','status':'timeout'},{'tool':'b','status':'ok'}]}
        state=update({},record)
        self.assertEqual((state['collector_status'],state['consecutive_errors']),('no_new_posts',0))
        self.assertEqual(update(state,record),state)
        token=reader_history.set(state['engines'])
        try:self.assertEqual(preferred(['a','b']),['b','a'])
        finally:reader_history.reset(token)
        for i in range(1,9):
            when=instant(record['checked_at'])+dt.timedelta(days=i)
            state=update(state,{**record,'checked_at':when.isoformat(),'status':'http_429'})
            self.assertLessEqual(instant(state['next_check_at'])-when,dt.timedelta(days=7))
        self.assertEqual(state['consecutive_errors'],8)
        self.assertLess(instant(state['next_check_at']),when+dt.timedelta(days=10))
        recovered=update(state,{**record,'checked_at':(when+dt.timedelta(days=10)).isoformat(),'new_keys':['telegram:example/5']})
        self.assertEqual(recovered['consecutive_errors'],0)
        self.assertEqual(recovered['last_new_post_at'],recovered['last_checked_at'])

    def test_transient_failure_retries_and_server_retry_after_wins(self):
        when='2026-11-01T00:00:00Z'
        row={'url':'https://news.test','platform':'rss','checked_at':when,'new_keys':[],
             'status':'http_503','seconds':1,'attempts':[]}
        old={'url':row['url'],'consecutive_errors':12}
        deadline=instant(update(old,row)['next_check_at'])
        self.assertLessEqual(deadline-instant(when),dt.timedelta(minutes=66))
        until='2026-11-02T02:00:00Z'
        self.assertEqual(instant(update(old,{**row,'retry_after':until})['next_check_at']),instant(until))

    def test_richer_observation_in_either_order_for_every_platform(self):
        for platform in run.PLATFORMS:
            with self.subTest(platform=platform):
                old={'platform':platform,'observed_at':'2026-01-01T00:00:00Z','text':'Author body',
                     'text_may_be_truncated':False,'published_at':'2025-12-31T10:00:00Z'}
                latest={**old,'observed_at':'2026-01-02T00:00:00Z','text':'Author…','text_may_be_truncated':True,'published_at':None}
                for left,right in ((old,latest),(latest,old)):
                    merged=merge_observations(left,right)
                    self.assertEqual((merged['text'],merged['published_at']),(old['text'],old['published_at']))

    def test_workers_receive_only_runtime_environment(self):
        with patch.dict('os.environ',{'ARGUS_VK_TOKEN':'fixture','ARGUS_DRIVE_OAUTH':'fixture','SOME_NEW_TOKEN':'fixture'}):
            self.assertFalse({'ARGUS_VK_TOKEN','ARGUS_DRIVE_OAUTH','SOME_NEW_TOKEN'}.intersection(worker_env()))

    def test_pipeline_reads_due_only_and_reconciles_skipped_sources(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from merge import merge
        with TemporaryDirectory() as directory:
            root=Path(directory)
            sources=[{'id':sid,'platform':'rss','url':'https://news.test/'+sid} for sid in ('overdue','later')]
            sources.append({'id':'x-later','platform':'x','url':'https://x.com/example'})
            catalog=root/'catalog.json'
            catalog.write_text(json.dumps({'provenance':'fixture','sources':sources}))
            checkpoint=root/'state.json'
            checkpoint.write_text(json.dumps({'schema':'argus-collection-cursors-v1','sources':{},'polling':{
                s['id']:{'url':s['url'],'next_check_at':year+'-01-01T00:00:00Z'}
                for s,year in zip(sources,('2000','2100','2100'))}}))
            args=SimpleNamespace(output=root/'argus-platform-part-0',sources=catalog,registry=None,cursors=checkpoint,
                adaptive=True,sources_per_platform=0,shard_count=1,shard_index=0,posts=5,media_fallback=False)
            raw=run.post(sources[0],'rss:article','https://news.test/article','Full article')
            adapter=AsyncMock(return_value=([raw],'ok',[{'tool':'rss','status':'ok','posts':1}]))
            runtime=MagicMock()
            runtime.__aenter__.return_value.chromium.launch=AsyncMock(return_value=AsyncMock())
            env={'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted','ARGUS_SOURCE_ADAPTERS':'1'}
            with patch.dict('os.environ',env,clear=True),patch('playwright.async_api.async_playwright',return_value=runtime),patch('run.collect_source',adapter),patch('source_adapters.prime_x_sources',new_callable=AsyncMock) as prime:
                asyncio.run(run.run(args))
            prime.assert_not_awaited()
            adapter.assert_awaited_once()
            merge(root,catalog,root/'merged',1)
            report=json.loads((root/'merged/report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['attempted_sources'],1)
            self.assertEqual({r['source_id']:r['status'] for r in report['sources']},{'later':'not_due','x-later':'not_due','overdue':'ok'})
