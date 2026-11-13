"""Real package, staged writer, catalog routing and durable cursor, with in-memory Drive."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from test_publish import MemoryStore
from publish import load_snapshot
from publish_incremental import drain
from run import run


class XDeliveryTests(unittest.TestCase):
    def test_archive_recovery_then_resume_via_normal_runner(self):
        source={'id':'x-fixture','platform':'x','name':'Example','url':'https://x.com/example','origin':'dsns'}
        catalog={'sources':[source]}
        store=MemoryStore()
        store.json('sources.json',catalog)
        runtime=MagicMock()
        runtime.__aenter__.return_value.chromium.launch=AsyncMock(return_value=AsyncMock())
        calls=[]
        def upstream(path,params,bearer):
            self.assertEqual(bearer,'fixture')
            if path.startswith('users/by/'):
                return {'data':{'id':'5','username':'example'}}
            calls.append(dict(params))
            ids=[30,29] if len(calls)==1 else [28]
            data=[{'id':str(i),'author_id':'5','text':str(i),'created_at':'2026-01-01T12:00:00Z',
                   'referenced_tweets':[{'type':'quoted','id':'19'}]} for i in ids]
            return {'data':data,'meta':{'result_count':len(ids),**({'next_token':'abc'} if len(calls)==1 else {})}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'sources.json').write_text(json.dumps(catalog))
            args=SimpleNamespace(output=root/'batch',sources=root/'sources.json',registry=None,adaptive=False,
                                 cursors=root/'state.json',incremental=True,window_hours=48,
                                 sources_per_platform=0,shard_count=1,shard_index=0,posts=1,media_fallback=False)
            for tick in (1,2):
                state=store.read_json('collection-cursors.json') or {'schema':'argus-collection-cursors-v1'}
                args.cursors.write_text(json.dumps(state))
                env={'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted','ARGUS_X_BEARER_TOKEN':'fixture',
                     'GITHUB_RUN_ID':str(tick)}
                with patch.dict(os.environ,env,clear=True),patch('playwright.async_api.async_playwright',return_value=runtime),patch('x_api.request',side_effect=upstream),patch('depth.policy',return_value=(1,1000,45)):
                    asyncio.run(run(args))
                report=json.loads((args.output/'report.json').read_bytes())
                pkg=args.output/'package.zip'
                if tick==1:
                    store.fail='snapshot-'
                    with self.assertRaises(RuntimeError):drain(store,pkg,report)
                    self.assertIsNone(store.read_json('collection-cursors.json'))
                    self.assertEqual(store.read_json('incremental-pending.json')['state'],'pending')
                    store.fail=None
                drain(store,pkg,report)
                drain(store,pkg,report)
            self.assertEqual(calls[1]['pagination_token'],'abc')
            items=load_snapshot(store)['items']
            self.assertEqual({r['id'] for r in items},{'x:30','x:29','x:28'})
            self.assertTrue(all(r['origin']=='dsns' and r['x_references']==[{'type':'quoted','id':'19'}] for r in items))
            current=store.read_json('collection-cursors.json')['incremental_sources']['x-fixture']['x_api']
            self.assertEqual(current,{'owner_id':'5','since_id':'30'})
            diag=store.read_json('monitor.json')['source_health']['x']
            self.assertEqual(diag['checked_24h'],1)
            self.assertEqual(diag['latest_poll_totals']['saved_new'],1)
            self.assertEqual(diag['detection_seconds_24h']['samples'],3)
