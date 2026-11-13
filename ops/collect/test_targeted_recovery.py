import asyncio
import ast
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock,patch
import urllib.error

import rss_gateway as gateway
from collector_preflight import unavailable_reason,instagram_gateway_candidate,incremental_route
from source_kind import video_scope

SOURCE={'id':'ig','platform':'instagram','url':'https://www.instagram.com/example'}


class GatewayAdmissionTests(unittest.TestCase):
    def setUp(self):
        for name,value in [('_lock',threading.Lock()),('_limited',False),('_last_finished',0)]:
            p=patch.object(gateway,name,value);p.start();self.addCleanup(p.stop)

    def test_busy_has_no_wait_no_fetch_and_no_queued_late_request(self):
        gateway._lock.acquire()
        with patch('run.fetch') as fetch,patch.object(gateway.time,'sleep',side_effect=AssertionError('must not sleep')):
            with self.assertRaisesRegex(ValueError,'gateway_busy'):gateway.collect(SOURCE,3)
            gateway._lock.release()
            fetch.assert_not_called()

    def test_cooldown_does_not_sleep_or_fetch_and_preserves_next_admission(self):
        metrics={}
        with patch.object(gateway,'_last_finished',100),patch.object(gateway.time,'monotonic',return_value=102),patch('run.fetch') as fetch:
            with self.assertRaisesRegex(ValueError,'gateway_cooldown'):gateway.collect(SOURCE,3,metrics=metrics)
            fetch.assert_not_called()
            self.assertEqual(metrics['gateway_retry_seconds'],3)
            self.assertFalse(metrics['gateway_admitted'])
        with patch.object(gateway,'_last_finished',100),patch.object(gateway.time,'monotonic',return_value=105),patch('run.fetch',return_value=('body',SOURCE['url'])) as fetch,patch.object(gateway,'parse',return_value=['verified']) as parse:
            self.assertEqual(gateway.collect(SOURCE,3,metrics=metrics),['verified'])
            fetch.assert_called_once();parse.assert_called_once()
            self.assertTrue(metrics['gateway_admitted'])

    def test_timeout_releases_lock_but_preserves_spacing(self):
        with patch('run.fetch',side_effect=TimeoutError),patch.object(gateway.time,'monotonic',return_value=100):
            with self.assertRaises(TimeoutError):gateway.collect(SOURCE,3)
        self.assertFalse(gateway._lock.locked())
        self.assertEqual(gateway._last_finished,100)
        with patch('run.fetch') as fetch,patch.object(gateway.time,'monotonic',return_value=101):
            with self.assertRaisesRegex(ValueError,'gateway_cooldown'):gateway.collect(SOURCE,3)
            fetch.assert_not_called()

    def test_429_keeps_circuit_closed_and_original_error_headers(self):
        error=urllib.error.HTTPError(gateway.GATEWAY,429,'private',{'Retry-After':'300'},None)
        with patch('run.fetch',side_effect=error) as fetch:
            with self.assertRaises(urllib.error.HTTPError) as caught:gateway.collect(SOURCE,3)
            self.assertIs(caught.exception,error)
            with self.assertRaisesRegex(ValueError,'gateway_rate_limited'):gateway.collect(SOURCE,3)
            self.assertEqual(fetch.call_count,1)


class AdapterPreflightTests(unittest.TestCase):
    def test_missing_primary_falls_back_without_network(self):
        import source_adapters as adapters
        source={'platform':'x','url':'https://x.com/example'}
        row={'canonical_key':'x:123'}
        legacy=AsyncMock(return_value=([row],'ok',[{'tool':'public_fixture','status':'ok','posts':1}]))
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless','ARGUS_ANONYMOUS_ONLY':'1'}),patch.object(adapters,'pravaha',side_effect=AssertionError('no loopback call')):
            items,status,attempts=asyncio.run(adapters.collect(None,source,3,False,legacy,None))
        legacy.assert_awaited_once()
        self.assertEqual((items,status),([row],'ok'))
        self.assertEqual(attempts[0]['reason'],'local_service_not_in_image')
        self.assertEqual(attempts[0]['status'],'tool_unavailable')

    def test_missing_threads_executable_and_unsupported_tools_do_not_probe(self):
        with patch.dict(os.environ,{'ARGUS_SOCIAL_TOOLS':'/not-installed'},clear=True):
            self.assertEqual(unavailable_reason('threads-cli'),'executable_missing')
            self.assertIsNone(unavailable_reason('social-fetch'))
            for tool in ('facebook-cli','threads-agent','python-social','remaining-python','short-post-python','telegramchannels'):
                self.assertEqual(unavailable_reason(tool),'executable_missing')

    def test_x_preflight_does_not_spend_pacing_budget_on_missing_service(self):
        import source_adapters as adapters
        sources=[{'id':str(i),'platform':'x','url':'https://x.com/example'+str(i)} for i in range(3)]
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'}),patch.object(adapters.asyncio,'sleep',new_callable=AsyncMock) as sleep:
            prepared,stopped=asyncio.run(adapters.prime_x_sources(sources,3,5))
        sleep.assert_not_awaited();self.assertEqual(len(prepared),3);self.assertFalse(stopped)
        self.assertTrue(all(r[1]['status']=='tool_unavailable' for r in prepared.values()))

    def test_gateway_scope_excludes_individual_posts_and_reserved_paths(self):
        self.assertTrue(instagram_gateway_candidate(SOURCE))
        for url in ['https://www.instagram.com/p/ABC/','https://www.instagram.com/reel/ABC/','https://www.instagram.com/accounts/','https://elsewhere.test/example']:
            self.assertFalse(instagram_gateway_candidate({**SOURCE,'url':url}))

    def test_actual_native_dispatch_preserves_rss_mailru_telegram_youtube(self):
        tree=ast.parse(Path(__file__).with_name('run.py').read_text())
        branches=[n for n in ast.walk(tree) if isinstance(n,ast.If)]
        for platform,module in [('rss','feed_history'),('mailru','feed_history'),('youtube','youtube_history'),('telegram','telegram_history')]:
            matches=[]
            for n in branches:
                imports=[child.module for stmt in n.body for child in ast.walk(stmt) if isinstance(child,ast.ImportFrom)]
                if module in imports and platform in ast.unparse(n.test):matches.append(n)
                elif module=='feed_history' and module in imports:matches.append(n)
            self.assertEqual(len(matches),1)
            self.assertEqual(incremental_route({'platform':platform}),{'rss':'public_feed_history','mailru':'public_feed_history','youtube':'youtube_public_history','telegram':'telegram_public_history'}[platform])

    def test_video_scope_review_does_not_rewrite_registry(self):
        examples=[('rutube','https://rutube.ru/live/video/abc','livestream'),('rutube','https://rutube.ru/channel/123/','channel'),('rutube','https://rutube.ru/plst/123/','playlist'),('youtube','https://youtube.com/playlist?list=123','playlist'),('youtube','https://youtube.com/feeds/videos.xml?channel_id=123','publication_feed'),('youtube','https://youtu.be/abc','video'),('tiktok','https://vt.tiktok.com/abc','unresolved_short_link')]
        for platform,url,expected in examples:
            source={'platform':platform,'url':url};before=dict(source)
            self.assertEqual(video_scope(source),expected);self.assertEqual(source,before)


class SourceMetricsTests(unittest.TestCase):
    def test_metrics_survive_health_update_without_fabricating_extraction_time(self):
        from source_health import update
        row={'url':SOURCE['url'],'platform':'instagram','status':'no_public_posts',
             'checked_at':'2026-11-13T12:00:00Z','new_keys':[],'canonical_keys':[],
             'attempts':[{'tool':'rss_bridge','status':'gateway_busy','posts':0,'gateway_admitted':False}],
             'seconds':1,'collector_rows':0,'rejections':[]}
        health=update({},row)
        self.assertIsNone(health['last_extraction_at'])
        self.assertEqual(health['pipeline']['rejected_records'],0)
        self.assertFalse(health['pipeline']['gateway'][0]['gateway_admitted'])
        row.update(collector_rows=1,canonical_keys=['instagram:ABC'],status='ok')
        health=update(health,{**row,'checked_at':'2026-11-13T12:05:00Z'})
        self.assertEqual(health['last_extraction_at'],'2026-11-13T12:05:00Z')
        self.assertEqual(health['actual_interval_seconds'],300)


class RouteInventoryTests(unittest.TestCase):
    def test_hostless_preflight_keeps_native_routes_and_names_missing_primary(self):
        from collector_preflight import route_inventory
        catalog={'sources':[{'platform':p,'url':'https://example.test'} for p in ['rss','mailru','telegram','youtube','x','threads']]}
        r=route_inventory(catalog,{'ARGUS_COLLECTOR_RUNTIME':'hostless','ARGUS_SOURCE_ADAPTERS':'1','DATABASE_URL':'SECRET'})
        rows={row['platform']:row for row in r['platforms']}
        self.assertEqual(rows['x']['primary_unavailable_reason'],'local_service_not_in_image')
        self.assertEqual(rows['rss']['incremental_route'],'public_feed_history')
        self.assertFalse(rows['x']['fallback_execution_verified'])
        self.assertNotIn('SECRET',str(r))
