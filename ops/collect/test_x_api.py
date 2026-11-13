import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from browser_public import normalize_rows
from import_sheet import normalize
from public_embeds import parse_x
from x_api import collect, target


def page(ids, token=None):
    return {'data':[{'id':str(i), 'author_id':'7', 'text':str(i),
                     'created_at':'2026-11-01T14:05:16+02:00'} for i in ids],
            'meta':{'result_count':len(ids), **({'next_token':token} if token else {})}}


class XTests(unittest.TestCase):
    profile = {'id':'p','platform':'x','url':'https://x.com/Alice'}
    search = {'id':'q','platform':'x','url':'https://twitter.com/search?q=%23news+from%3ABob'}
    user = {'data':{'username':'Alice','id':'7'}}

    def batch(self, source, cursor, read, pages=4):
        return collect(source, cursor, 'test', max_pages=pages, max_posts=1000, seconds=30, read=read)

    def test_resume_after_throttle_keeps_completed_pages_and_anchor(self):
        old = {'x_api':{'since_id':'1','owner_id':'7'}}
        read = Mock(side_effect=[self.user, page(range(202,102,-1),'abc'), HTTPError('',429,'',{},None)])
        rows,status,_,update = self.batch(self.profile,old,read)
        self.assertEqual((len(rows),status,update['x_api']['since_id']), (100,'http_429','1'))
        second = Mock(side_effect=[self.user,page(range(102,2,-1),'def'),page([2])])
        more,status,_,finished = self.batch(self.profile,update,second)
        self.assertEqual((status,finished['x_api']),('ok',{'since_id':'202','owner_id':'7'}))
        self.assertEqual(len({r['canonical_key'] for r in rows+more}),201)
        self.assertEqual(rows[0]['published_at'],'2026-11-01T12:05:16+00:00')
        next_tick=Mock(side_effect=[self.user,page([203])])
        self.batch(self.profile,finished,next_tick)
        self.assertEqual(next_tick.call_args.args[1]['since_id'],'202')

    def test_search_token_expiry_and_identity(self):
        _,url=normalize(self.search['url']+'&src=typed_query')
        self.assertEqual(target({**self.search,'url':url}),('search','#news from:Bob'))
        self.assertIsNone(target({**self.search,'url':'https://x.com/search'}))
        got=[]
        def read(path,params,bearer):
            got.append(dict(params))
            if len(got)==1:return page([14,13],'expired')
            if 'next_token' in params:raise HTTPError('',400,'',{},None)
            return page([12])
        _,_,_,checkpoint=self.batch(self.search,{},read,pages=1)
        rows,status,_,done=self.batch(self.search,checkpoint,read)
        self.assertEqual((status,len(rows),done['x_api']['since_id']),('ok',1,'14'))
        self.assertEqual(got[-1]['until_id'],'13')
        self.assertEqual(got[-1]['query'],'#news from:Bob')
        self.assertEqual(rows[0]['post_url'],'https://x.com/i/web/status/12')

    def test_types_media_and_empty_page_with_continuation(self):
        data=page([44,43,42,41])
        data['data'][1]['conversation_id']='2'
        for t,kind in zip(data['data'],['retweeted','replied_to','quoted']):
            t['referenced_posts']=[{'type':kind,'id':'2'}]
        data['data'][-1].update(text='',attachments={'media_keys':['v']})
        data['includes']={'media':[{'media_key':'v','preview_image_url':'https://pbs.twimg.com/a',
                                     'variants':[{'url':'https://video.twimg.com/v','content_type':'video/mp4'}]}]}
        rows,status,attempts,_=self.batch(self.profile,{},Mock(side_effect=[self.user,page([],'abc'),data]))
        self.assertEqual((len(rows),status,attempts[0]['pages']),(4,'ok',2))
        self.assertEqual(rows[1]['conversation_id'],'2')
        self.assertEqual(rows[1]['platform_post_id'],'43')
        self.assertEqual([r['x_references'] for r in rows[:3]],[[{'type':k,'id':'2'}] for k in ('retweeted','replied_to','quoted')])
        self.assertEqual(rows[-1]['media_urls'],['https://video.twimg.com/v','https://pbs.twimg.com/a'])

    def test_incomplete_page_is_retried_without_advancing_head(self):
        bad=page([8]);bad['errors']=[{'title':'Partial response'}]
        rows,status,_,checkpoint=self.batch(self.profile,{},Mock(side_effect=[self.user,bad]))
        self.assertEqual(([r['canonical_key'] for r in rows],status,checkpoint),(['x:8'], 'x_incomplete_response', None))

    def test_snapshot_does_not_lose_fresh_post_after_pinned_record(self):
        entries=[{'content':{'tweet':{'id_str':str(i),'user':{'screen_name':'Alice'},'text':str(i)}}} for i in [1,5,3]]
        html='<script id="__NEXT_DATA__">'+json.dumps({'props':{'pageProps':{'timeline':{'entries':entries}}}})+'</script>'
        self.assertEqual([r['canonical_key'] for r in parse_x(html,self.profile,1)],['x:5','x:3','x:1'])

    def test_search_card_and_permalink_cannot_mix_statuses(self):
        raw=[{'url':'https://x.com/Alice/status/20','text':'one'},{'url':'https://x.com/Bob/status/21','text':'two'}]
        self.assertEqual(len(normalize_rows(self.search,raw,10)),2)
        self.assertEqual(len(normalize_rows(self.profile,raw,10)),1)
        self.assertEqual(normalize_rows({**self.profile,'url':self.profile['url']+'/status/19'},raw,10),[])

    def test_embed_robots_denial_stops_collector_chain(self):
        import asyncio
        from unittest.mock import AsyncMock
        from run import collect_existing
        with patch('crawl_common.CrawlState.raw',return_value=(b'User-agent: *\nDisallow: /','')), patch('run.social',new_callable=AsyncMock) as browser:
            rows,status,attempts=asyncio.run(collect_existing(None,self.profile,30))
        self.assertEqual((rows,status),([], 'robots_denied'))
        browser.assert_not_called()


    def test_x_scheduler_honors_quota_reset_after_repeated_failures(self):
        from source_health import update
        row={'platform':'x', 'url':self.profile['url'], 'checked_at':'2026-11-03T12:00:00+00:00',
             'status':'http_429','new_keys':[], 'attempts':[], 'seconds':1, 'retry_after':'2026-11-03T12:17:00+00:00'}
        updated=update({'url':row['url'],'consecutive_errors':12},row)
        self.assertEqual(updated['next_check_at'],row['retry_after'])
        self.assertEqual(updated['host_cooldown_until'],row['retry_after'])

    def test_expired_search_range_is_reported_and_fresh_head_can_resume(self):
        old={'x_api':{'start_time':'2020-01-01T00:00:00Z','next_token':'v','pending_head':'48'}}
        read=Mock(return_value=page([77]))
        rows,_,attempts,update=self.batch(self.search,old,read)
        self.assertNotIn('next_token',read.call_args.args[1])
        self.assertEqual(attempts[0]['unavailable_range']['reason'],'x_search_retention')
        self.assertEqual(update['x_api']['since_id'],'77')

    def test_search_rediscovery_preserves_verified_publisher_feed(self):
        from record_quality import merge_observations
        old={'platform':'x','source_url':self.profile['url'],'origin':'dsns','source_id':'owner','observed_at':'2026-10-01T10:00:00Z'}
        later={**old,'source_url':self.search['url'],'origin':'general','source_id':'query','observed_at':'2026-10-01T11:00:00Z'}
        merged=merge_observations(old,later)
        self.assertEqual((merged['source_id'],merged['origin']),('owner','dsns'))
