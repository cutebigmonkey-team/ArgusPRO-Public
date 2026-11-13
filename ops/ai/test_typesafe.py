import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'drive'))
from typesafe import ApiError, QUESTIONS, MODEL, request_body, validate, retry_seconds
from worker import Worker, LEDGER, approved_text, content_hash
from reporting import build_reports
from privacy import verified_public

NOW=1793361600.0  # Synthetic clock; no private archive data in tests.
POST={'id':'test:1','text':'Рятувальна операція завершена.','platform':'rss','url':'https://fixture.invalid/source/19news/1',
      'published_at':dt.datetime.fromtimestamp(NOW-3600,dt.timezone.utc).isoformat(),'source_name':'ДСНС','files':[]}
SNAPSHOT={'items':[POST],'packages':[],'updated_at':'2026-10-30T12:00:00+00:00'}

def response():
    answers={}
    for key,q in QUESTIONS.items():
        if q['type']=='noul': answers[key]={'type':'noul','noul':.95}
        else:
            options=list(q['criteria']);selected=options[0]
            answers[key]={'type':'choice','choice':selected,'confidence':.9,'probabilities':{o:1.0 if o==selected else 0.0 for o in options}}
    return {'model':MODEL,'answers':answers,'usage':{'input_tokens':200,'output_tokens':40}}

def policy(post=POST,text='Рятувальна операція завершена.'):
    return {'publications':{post['id']:{'approved':True,'classification':'public_nonpersonal','reviewer':'test reviewer',
        'reviewed_at':'2026-10-30T10:00:00Z','expires_at':'2030-01-01T00:00:00Z','text':text,'content_sha256':content_hash(post)}}}

class Store:
    def __init__(self): self.files={};self.fail=False
    def assert_private(self): pass
    def read_json(self,name): return copy.deepcopy(self.files.get(name))
    def json(self,name,data,mutable=False):
        if self.fail: raise RuntimeError('checkpoint failure')
        self.files[name]=copy.deepcopy(data);return 'file_'+str(len(self.files)).zfill(12)

class Client:
    def __init__(self,error=None): self.calls=[];self.error=error
    def call(self,body):
        self.calls.append(json.loads(body))
        if self.error: raise self.error
        return response()

class ContractTests(unittest.TestCase):
    def test_explicit_official_consent_checks_identity_not_name(self):
        p={**POST,'platform':'telegram','url':'https://t.me/fixture123/123','text':'Публічно повідомляється про допомогу. Ім’я: Олена.'}
        channels=frozenset({'dsns_telegram'})
        self.assertIsNone(approved_text(p,{},NOW))
        self.assertEqual(approved_text(p,{},NOW,True,channels),p['text'])
        for url in ('https://t.me/impostor/123','https://dsns.gov.ua.evil.example/a','https://dsns.gov.ua@evil.example/a','https://t.me/fixture123/123?comment=1'):
            self.assertIsNone(approved_text({**p,'url':url},{},NOW,True,channels))
        self.assertIsNone(approved_text({**p,'text':'Для службового користування'},{},NOW,True,channels))
        self.assertIsNone(approved_text({**p,'text_may_be_truncated':True},{},NOW,True,channels))
        self.assertIsNone(approved_text({**p,'sample':True},{},NOW,True,channels))
    def test_native_api_contract_and_no_metadata(self):
        body=json.loads(request_body('Public synthetic text'))
        self.assertEqual(set(body),{'model','state','questions'})
        self.assertEqual(body['model'],MODEL)
        self.assertEqual(body['questions']['topic']['type'],'choice')
        self.assertEqual(validate(response())['assessments']['critical']['value'],True)
    def test_malformed_uncertain_and_low_confidence(self):
        data=response();data['answers']['critical']['noul']=float('nan')
        with self.assertRaises(ValueError):validate(data)
        data=response();data['answers']['topic']['confidence']=.3
        self.assertIsNone(validate(data)['assessments']['topic']['value'])
        self.assertTrue(validate(data)['needs_review'])
        data=response();del data['usage']['input_tokens']
        with self.assertRaises(ValueError):validate(data)
    def test_retry_header_and_size(self):
        self.assertEqual(retry_seconds('120'),120)
        self.assertGreater(retry_seconds('Wed, 21 Oct 2030 07:28:00 GMT',NOW),120)
        with self.assertRaises(ValueError):request_body('я'*24000)
        self.assertEqual(json.loads(request_body('я'*24000,max_bytes=65536))['state'],'я'*24000)
    def test_privacy_defaults_edit_expiry_and_regex(self):
        self.assertIsNone(approved_text(POST,{},NOW))
        changed={**POST,'text':'Changed'}
        self.assertIsNone(approved_text(changed,policy(),NOW))
        self.assertIsNone(approved_text(POST,policy(text='person@example.org'),NOW))
        self.assertIsNone(approved_text(POST,policy(text='+380 67 123 4567'),NOW))
        self.assertIsNone(approved_text(POST,policy(),2000000000))
        self.assertEqual(approved_text(POST,policy(),NOW),'Рятувальна операція завершена.')

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def worker(self,store,client,**config):
        return Worker(store,client,{'requests_per_second':100,'tokens_per_second':1000000,**config},clock=lambda:NOW)
    async def test_expired_slot_publishes_cache_without_reserving_or_sending(self):
        store=Store();client=Client()
        result=await self.worker(store,client).run(SNAPSHOT,policy(),max_seconds=0)
        self.assertEqual(client.calls,[])
        self.assertEqual(result['requests_this_run'],0)
        self.assertEqual(result['pending_eligible'],1)

    async def test_slow_preparation_leaves_a_bounded_request_window(self):
        for overall, window, expected in ((45,None,0),(180,90,1),(60,90,0)):
            stamp=[100.0];store=Store();client=Client()
            def prepare():stamp[0]+=60
            store.assert_private=prepare
            fake_time=SimpleNamespace(monotonic=lambda:stamp[0],sleep=lambda seconds:None)
            with patch('worker.time',fake_time):
                result=await self.worker(store,client).run(SNAPSHOT,policy(),max_seconds=overall,request_seconds=window)
            self.assertEqual(len(client.calls),expected)
            self.assertEqual(result['timings']['prepare_seconds'],60)
            if not expected:self.assertEqual(result['stop_reason'],'slot_deadline')

    async def test_parallel_wave_is_durably_reserved_and_shares_daily_budget(self):
        store=Store();client=Client();call=client.call
        barrier=threading.Barrier(2)
        def simultaneous(body):
            jobs=list(store.files[LEDGER]['jobs'].values())
            self.assertEqual(sum(j['status']=='inflight' for j in jobs),2)
            self.assertEqual(next(iter(store.files[LEDGER]['days'].values()))['requests'],2)
            barrier.wait(timeout=3)
            return call(body)
        client.call=simultaneous
        rows=[{**POST,'id':str(n),'text':'Synthetic publication '+str(n)} for n in range(6)]
        config={'public_dsns_consent':True,'concurrency':2,'daily_requests':2,'daily_input_tokens':100000}
        result=await self.worker(store,client,**config).run({'items':rows},{},max_seconds=180,request_seconds=90)
        self.assertEqual(result['new_completed'],2)
        self.assertEqual(len(client.calls),2)
        self.assertEqual(result['requests_today'],2)
        # A separate consumer (e.g. comments) must see the same exhausted budget.
        other=Client()
        result=await self.worker(store,other,**config).run({'items':rows},{})
        self.assertEqual(other.calls,[])
        self.assertEqual(result['stop_reason'],'daily_budget')

    async def test_failed_product_readback_does_not_advance_pointer(self):
        store=Store();client=Client();read=store.read_json
        store.read_json=lambda name: None if name.startswith('typesafe-') and name.endswith('.json') and name != LEDGER else read(name)
        with self.assertRaisesRegex(ValueError,'typesafe_readback_failed'):
            await self.worker(store,client).run(SNAPSHOT,policy())
        self.assertNotIn('typesafe-catalog.json',store.files)
        self.assertEqual(next(iter(store.files[LEDGER]['jobs'].values()))['status'],'completed')

    async def test_lost_writer_guard_prevents_requests(self):
        store=Store();client=Client()
        def guard():raise RuntimeError('session lost')
        with self.assertRaises(RuntimeError):
            await Worker(store,client,guard=guard).run(SNAPSHOT,policy())
        self.assertEqual(client.calls,[])
    async def test_target_filters_requests_but_publishes_every_record(self):
        store=Store();client=Client()
        server=self.worker(store,client,public_dsns_consent=True)
        rows=[{**POST,'id':'other','text':'A different publication'},POST]
        await server.run({**SNAPSHOT,'items':rows},{},{POST['id']})
        self.assertEqual([c['state'] for c in client.calls],[POST['text']])
        product=next(v for v in store.files.values() if v.get('format')=='argus-typesafe-v1')
        self.assertEqual(set(product['assessments']),{POST['id'],'other'})
        self.assertEqual(product['assessments']['other']['status'],'pending_budget')
    async def test_anonymous_archive_consent_is_separate_and_only_sends_publication_text(self):
        item={**POST,'url':'https://publisher.org/news/1','source_url':'https://publisher.org/rss',
              'source_id':'publisher','tool':'native_feed','capture_mode':'native_feed','text_may_be_truncated':False,
              'public_comments':[{'text':'not the publication'}],'files':[{'name':'private attachment'}]}
        self.assertIsNone(approved_text(item,{},NOW,True))
        client=Client();store=Store()
        await self.worker(store,client,public_archive_consent=True).run({**SNAPSHOT,'items':[item]}, {})
        self.assertEqual([c['state'] for c in client.calls],[item['text']])
        self.assertNotIn('private attachment',json.dumps(client.calls))
        self.assertNotIn('not the publication',json.dumps(client.calls))
        for change in ({'text_may_be_truncated':True},{'text_may_be_truncated':None},{'sample':True},
                       {'visibility':'private'},{'internal':True},{'confidential':True},{'source_match':'search_query_only'},
                       {'capture_mode':'search_preview'},{'tool':'public_news_sitemap','capture_mode':'public_news_sitemap'},
                       {'source_url':'https://another.org/rss'},{'source_id':''},{'url':'https://u:p@publisher.org/secret'},
                       {'source_url':'https://publisher.org/rss?token=private'},{'retained_text_observed_at':'yesterday'},
                       {'video_analysis_text':'derived'},{'text':'ДЛЯ СЛУЖБОВОГО КОРИСТУВАННЯ'}):
            with self.subTest(change=change):self.assertIsNone(approved_text({**item,**change},{},NOW,public_archive_consent=True))
        self.assertEqual((await self.worker(store,client).run({**SNAPSHOT,'items':[item]},{}))['completed'],0)
    def test_anonymous_telegram_reader_binds_public_post_and_channel(self):
        item={**POST,'platform':'telegram','tool':'telegram_public_history','id':'telegram:public_channel/42',
              'source_id':'publisher','source_url':'https://t.me/public_channel','url':'https://t.me/public_channel/42'}
        self.assertEqual(approved_text(item,{},NOW,public_archive_consent=True),item['text'])
        for change in ({'url':'https://t.me/c/42'},{'url':'https://t.me/public_channel/41'},
                       {'source_url':'https://t.me/s/another_channel'},{'url':item['url']+'?comment=1'},
                       {'tool':'unknown'},{'text_may_be_truncated':True}):
            self.assertIsNone(approved_text({**item,**change},{},NOW,public_archive_consent=True))
    async def test_weekly_queue_prefers_recent_eligible_text_even_if_its_duplicate_is_old(self):
        store=Store();client=Client()
        old='2020-01-01T00:00:00Z'
        items=[{**POST,'id':'older','published_at':old,'text':'Older'},
               {**POST,'id':'copy','published_at':old},
               {**POST,'id':'future','published_at':'2030-01-01T00:00:00Z','text':'Future'},POST]
        await self.worker(store,client,max_posts=1,public_dsns_consent=True).run({**SNAPSHOT,'items':items},{})
        self.assertEqual([c['state'] for c in client.calls],[POST['text']])
        self.assertEqual(len(store.files[LEDGER]['jobs']),1)
    async def test_large_request_does_not_block_a_smaller_approved_one(self):
        store=Store();client=Client()
        long={**POST,'id':'long','text':'A'*3000}
        limit=len(request_body(POST['text']))
        await self.worker(store,client,public_dsns_consent=True,daily_input_tokens=limit,max_posts=1).run(
            {**SNAPSHOT,'items':[long,POST]}, {})
        self.assertEqual([c['state'] for c in client.calls],[POST['text']])
        budget=next(iter(store.files[LEDGER]['days'].values()))
        self.assertEqual(budget['reserved_tokens'],limit)
    async def test_replay_dedup_and_private_snapshot_binding(self):
        store=Store();client=Client();w=self.worker(store,client)
        await w.run(SNAPSHOT,policy());await self.worker(store,client).run(SNAPSHOT,policy())
        self.assertEqual(len(client.calls),1)
        self.assertEqual(client.calls[0]['state'],'Рятувальна операція завершена.')
        product=next(v for k,v in store.files.items() if k.startswith('typesafe-') and v.get('format')=='argus-typesafe-v1')
        self.assertEqual(product['assessments'][POST['id']]['status'],'completed')
        self.assertNotIn('catalog.json',store.files)
    async def test_no_egress_without_approval_and_budget(self):
        for policy_value,config in [({},{}),(policy(),{'daily_input_tokens':1})]:
            store=Store();client=Client();await self.worker(store,client,**config).run(SNAPSHOT,policy_value)
            self.assertEqual(client.calls,[])
    async def test_checkpoint_failure_never_calls_api(self):
        store=Store();store.fail=True;client=Client()
        with self.assertRaises(RuntimeError):await self.worker(store,client).run(SNAPSHOT,policy())
        self.assertFalse(client.calls)
    async def test_lost_checkpoint_reply_retries_storage_without_repeating_api(self):
        for failed_status in ('inflight', 'completed'):
            with self.subTest(failed_status=failed_status):
                store=Store();client=Client();write=store.json;failed=False
                def flaky(name,data,mutable=False):
                    nonlocal failed
                    result=write(name,data,mutable)
                    if name==LEDGER and not failed and next(iter(data['jobs'].values()))['status']==failed_status:
                        failed=True
                        raise ConnectionResetError('lost Drive reply')
                    return result
                store.json=flaky
                with patch('worker.time.sleep'):
                    await self.worker(store,client).run(SNAPSHOT,policy())
                await self.worker(store,client).run(SNAPSHOT,policy())
                self.assertTrue(failed)
                self.assertEqual(len(client.calls),1)
                self.assertEqual(next(iter(store.files[LEDGER]['jobs'].values()))['status'],'completed')
    async def test_repeated_network_checkpoint_failure_never_calls_api(self):
        store=Store();client=Client()
        with patch.object(store,'json',side_effect=ConnectionResetError('offline')) as save, patch('worker.time.sleep'):
            with self.assertRaises(ConnectionResetError):
                await self.worker(store,client).run(SNAPSHOT,policy())
        self.assertEqual(save.call_count,4)
        self.assertFalse(client.calls)
    async def test_ambiguous_and_inflight_requests_never_replay(self):
        store=Store();client=Client(ApiError('ambiguous_transport'))
        await self.worker(store,client).run(SNAPSHOT,policy())
        await self.worker(store,client).run(SNAPSHOT,policy())
        self.assertEqual(len(client.calls),1)
        next(iter(store.files[LEDGER]['jobs'].values()))['status']='inflight'
        await self.worker(store,client).run(SNAPSHOT,policy())
        self.assertEqual(len(client.calls),1)
    async def test_429_defers_honors_retry_after_and_reserves_cost(self):
        store=Store();client=Client(ApiError(429,4000))
        await self.worker(store,client).run(SNAPSHOT,policy())
        job=next(iter(store.files[LEDGER]['jobs'].values()))
        self.assertEqual(job['status'],'retry');self.assertGreaterEqual(job['retry_at'],NOW+4000)
        await self.worker(store,client).run(SNAPSHOT,policy());self.assertEqual(len(client.calls),1)
        self.assertGreater(next(iter(store.files[LEDGER]['days'].values()))['reserved_tokens'],0)
    async def test_changed_or_revoked_approval_hides_cached_result(self):
        store=Store();client=Client();await self.worker(store,client).run(SNAPSHOT,policy())
        result=await self.worker(store,client).run(SNAPSHOT,{})
        self.assertEqual(result['completed'],0);self.assertEqual(len(client.calls),1)

class ReportTests(unittest.TestCase):
    def test_sql_counts_no_fake_metrics_events_or_sample_posts(self):
        posts=[POST,{**POST,'id':'test:2','sample':True},{**POST,'id':'test:3','published_at':None}]
        analysis={'test:1':{'status':'completed',**validate(response())}}
        reports=build_reports({'items':posts},analysis,SNAPSHOT['updated_at'])
        report=next(r for r in reports if r['filters']['period']=='all' and r['filters']['platform']=='all' and r['filters']['region']=='all' and not r['filters']['dsns'])
        s=report['statistics'];self.assertEqual(s['posts'],2);self.assertEqual(s['sources'],1)
        self.assertEqual(s['assessed'],1);self.assertEqual(s['undated'],1);self.assertIsNone(s['engagement']);self.assertIsNone(s['views'])
        self.assertEqual(report['topics'][0]['count'],1)
    def test_official_report_uses_catalog_origin_even_when_name_or_assessment_suggests_dsns(self):
        items=[{**POST,'id':origin,'origin':origin} for origin in ('dsns','general','unknown')]
        reports=build_reports({'items':items},{'general':{'official_source':True}},'2026-10-30T12:00:00Z')
        counts={r['filters']['dsns']:r['statistics']['posts'] for r in reports
                if r['filters']['period']=='all' and r['filters']['platform']=='all' and r['filters']['region']=='all'}
        self.assertEqual(counts,{True:1,False:3})

    def test_custom_window_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):build_reports(SNAPSHOT,{},SNAPSHOT['updated_at'],[{'from':'2026-10-01','to':'2026-10-30'}])

if __name__=='__main__':unittest.main()
