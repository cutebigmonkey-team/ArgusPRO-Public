import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch
import run


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_telegram_source_publishes_head_before_following_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source={'id':'telegram','url':'https://t.me/example','platform':'telegram','_lease_token':'token'}
            catalog=root/'sources.json';catalog.write_text(json.dumps({'sources':[source]}))
            args=SimpleNamespace(sources=catalog,output=root/'result',registry=None,cursors=None,
                adaptive=False,incremental=True,telegram_history=True,sources_per_platform=0,
                shard_count=1,shard_index=0,posts=5,media_fallback=False,stream_results=True,
                concurrency=1,source_timeout=65)
            body=('<div class="tgme_widget_message" data-post="example/42">'
                  '<div class="tgme_widget_message_text">Verified publication</div>'
                  '<time datetime="'+run.now()+'"></time></div>'
                  '<a class="tme_messages_more" href="/s/example?before=42">Older</a>').encode()
            runtime=MagicMock();runtime.__aenter__.return_value.chromium.launch=AsyncMock()
            async def metrics(source,items,previous):return items,None
            with patch('runtime_policy.require_cloud'),patch('cloud_social.require_cloud'),patch('playwright.async_api.async_playwright',return_value=runtime),patch('post_metrics.bounded_refresh',side_effect=metrics),patch('run.fetch',return_value=(body,'https://t.me/s/example')) as fetch:
                await run.run(args)
            self.assertEqual(fetch.call_count,1)
            payload=json.loads(next((root/'result/spool').glob('*.json')).read_text())
            self.assertEqual(payload['record']['status'],'ok')
            self.assertEqual(len(payload['posts']),1)
            self.assertEqual(payload['state_update']['before'],42)
            self.assertEqual(payload['state_update']['head_id'],42)

    async def test_telegram_pagination_uses_remaining_request_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source={'id':'telegram','url':'https://t.me/example','platform':'telegram','_lease_token':'token'}
            catalog=root/'sources.json';catalog.write_text(json.dumps({'sources':[source]}))
            args=SimpleNamespace(sources=catalog,output=root/'result',registry=None,cursors=None,
                adaptive=False,incremental=True,telegram_history=True,sources_per_platform=0,
                shard_count=1,shard_index=0,posts=5,media_fallback=False,stream_results=True,
                concurrency=1,source_timeout=10.5)
            post=run.post(source,'telegram:example/42','https://t.me/example/42','Verified publication',run.now())
            def history(*args,fetcher,**kwargs):
                time.sleep(.05)
                fetcher('https://t.me/s/example?before=42')
                return [post],'ok',[{'tool':'telegram_public_history','status':'ok','posts':1}],None
            runtime=MagicMock();runtime.__aenter__.return_value.chromium.launch=AsyncMock()
            with patch('runtime_policy.require_cloud'),patch('playwright.async_api.async_playwright',return_value=runtime),patch('telegram_history.collect',side_effect=history),patch('post_metrics.bounded_refresh',new=AsyncMock(return_value=([post],None))),patch('run.fetch',return_value=(b'page','https://t.me/s/example')) as fetch:
                await run.run(args)
            self.assertGreater(fetch.call_args.kwargs['timeout'],0)
            self.assertLess(fetch.call_args.kwargs['timeout'],.25)
            payload=json.loads(next((root/'result/spool').glob('*.json')).read_text())
            self.assertEqual(len(payload['posts']),1)

    async def test_collected_telegram_posts_survive_slow_optional_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source={'id':'telegram','url':'https://t.me/example','platform':'telegram','_lease_token':'token'}
            catalog=root/'sources.json';catalog.write_text(json.dumps({'sources':[source]}))
            args=SimpleNamespace(sources=catalog,output=root/'result',registry=None,cursors=None,
                adaptive=False,incremental=True,telegram_history=True,sources_per_platform=0,
                shard_count=1,shard_index=0,posts=5,media_fallback=False,stream_results=True,
                concurrency=1,source_timeout=.1)
            post=run.post(source,'telegram:example/42','https://t.me/example/42','Verified publication',run.now())
            history=([post],'ok',[{'tool':'telegram_public_history','status':'ok','posts':1}],None)
            async def slow_metrics(*args):await asyncio.sleep(.2)
            runtime=MagicMock();runtime.__aenter__.return_value.chromium.launch=AsyncMock()
            with patch('runtime_policy.require_cloud'),patch('playwright.async_api.async_playwright',return_value=runtime),patch('telegram_history.collect',return_value=history),patch('post_metrics.bounded_refresh',side_effect=slow_metrics):
                await run.run(args)
            payload=json.loads(next((root/'result/spool').glob('*.json')).read_text())
            self.assertEqual(payload['record']['status'],'ok')
            self.assertEqual([p['canonical_key'] for p in payload['posts']],['telegram:example/42'])

    async def test_fast_result_is_exported_before_slow_collector_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            sources=[{'id':name,'url':f'https://{name}.test/feed','platform':'rss','_lease_token':name} for name in ('fast','slow')]
            catalog=root/'sources.json';catalog.write_text(json.dumps({'sources':sources}))
            args=SimpleNamespace(sources=catalog,output=root/'result',registry=None,cursors=None,
                adaptive=False,incremental=False,sources_per_platform=0,shard_count=1,shard_index=0,
                posts=5,media_fallback=False,stream_results=True,concurrency=4,source_timeout=.15)
            async def adapter(browser,source,*a,**kw):
                if source['id']=='slow':await asyncio.sleep(10)
                post=run.post(source,'rss:fast','https://fast.test/story','Fixture article')
                return [post],'ok',[{'tool':'fixture','status':'ok','posts':1}]
            runtime=MagicMock();runtime.__aenter__.return_value.chromium.launch=AsyncMock()
            with patch('runtime_policy.require_cloud'),patch('playwright.async_api.async_playwright',return_value=runtime),patch('run.collect_source',side_effect=adapter):
                task=asyncio.create_task(run.run(args))
                await asyncio.sleep(.05)
                self.assertFalse(task.done())
                files=list((root/'result/spool').glob('*.json'))
                self.assertEqual(len(files),1)
                exported=json.loads(files[0].read_text(encoding='utf-8'))
                self.assertEqual(exported['record']['source_id'],'fast')
                self.assertEqual(len(exported['posts']),1)
                await task
            rows=[json.loads(p.read_text(encoding='utf-8'))['record'] for p in (root/'result/spool').glob('*.json')]
            self.assertEqual({r['source_id']:r['status'] for r in rows},{'fast':'ok','slow':'timeout'})
            runtime.__aenter__.return_value.chromium.launch.assert_not_awaited()
