"""Measure 20 catalog websites; artifacts only, no archive write or credentials."""
import asyncio
import collections
import importlib.metadata
import json
import os
from pathlib import Path
import time
from enhanced import collect,config
from run import error_code,fetch,feed_posts


async def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('Probe requires GitHub-hosted Actions')
    root=Path('collection/enhanced-probe');root.mkdir(parents=True,exist_ok=True)
    sources=json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))['sources']
    selected=[s for s in sources if s['platform']=='rss' and s.get('enabled',True)][:20]
    if len(selected)!=20:raise ValueError('expected_twenty_catalog_sources')
    settings=config();results=[];posts={};gate=asyncio.Semaphore(2);started=time.monotonic()
    async def one(source):
        async with gate:
            start=time.monotonic()
            engine=settings['source_engines'].get(source['id'],settings['default_engine'])
            try:items,diagnostic=await collect(source,5,settings)
            except Exception as error:items=[];diagnostic={'tool':engine,'status':error_code(error),'posts':0}
            results.append({'source_id':source['id'],'url':source['url'],**diagnostic,
                'seconds':round(time.monotonic()-start,2),'text_characters':sum(len(p['text']) for p in items),
                'markdown_posts':sum(bool(p.get('markdown')) for p in items)})
            for item in items:posts[item['canonical_key']]=item
            print(json.dumps(results[-1]),flush=True)
    await asyncio.gather(*(one(s) for s in selected))
    # Historically published RSS URL; one direct cloud request, no guessed API
    # paths or challenge-solving. Its result is separate from website success.
    dsns_source=next((s for s in selected if s['url'].rstrip('/')=='https://dsns.gov.ua'),None)
    official_feed={'url':'https://fixture.invalid/source/19ua/rss.xml','status':'source_not_in_catalog'}
    if dsns_source:
        official_feed['source_id']=dsns_source['id']
        try:
            body,final=await asyncio.wait_for(asyncio.to_thread(fetch,official_feed['url'],12),timeout=15)
            from browser_public import page_error
            failure=page_error(final,body.decode('utf-8','replace'))
            feed_items=[] if failure else feed_posts(dsns_source,body,5)
            official_feed.update(status=failure or ('ok' if feed_items else 'no_public_posts'),posts=len(feed_items))
        except Exception as error:official_feed.update(status=error_code(error),posts=0)
    from rsshub_adapter import channel,collect as rsshub_collect
    gateway_source=next((s for s in sources if s.get('enabled',True) and channel(s)),None)
    gateway={'status':'no_supported_catalog_channel'}
    if gateway_source:
        try:
            gateway_posts=await asyncio.wait_for(asyncio.to_thread(rsshub_collect,gateway_source,5,settings['rsshub_base']),timeout=20)
            gateway={'status':'ok' if gateway_posts else 'no_verified_posts','posts':len(gateway_posts)}
        except Exception as error:gateway={'status':error_code(error),'posts':0}
        gateway['source_id']=gateway_source['id']
    report={'schema':'argus-enhanced-probe-v1','sources':results,'unique_posts':len(posts),
        'rsshub_probe':gateway,
        'dsns_official_feed_probe':official_feed,
        'seconds':round(time.monotonic()-started,2),'statuses':dict(collections.Counter(r['status'] for r in results)),
        'versions':{p:importlib.metadata.version(p) for p in ('trafilatura','crawlee','crawl4ai','Scrapy')}}
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'posts.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in posts.values()),encoding='utf-8')
    if any(r['status'] in ('crawler_worker_failed','collection_failed','crawler_handler_failed')
           or 'crawler_handler_failed' in r.get('errors',[]) for r in results):
        raise RuntimeError('Crawler integration failure; inspect report')


if __name__=='__main__':asyncio.run(main())
