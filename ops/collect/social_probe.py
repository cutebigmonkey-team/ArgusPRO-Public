"""Measure every Telegram/Threads catalog source and exercise both Threads tools."""
import asyncio
import collections
import json
import os
from pathlib import Path
import time

from cloud_social import require_cloud, collect, threads, library, diagnostic
from run import now, error_code


async def main():
    require_cloud()
    root = Path('collection/social-probe'); root.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(Path('ops/collect/sources.json').read_text(encoding='utf-8'))
    sources = [s for s in catalog['sources'] if s['platform'] in ('telegram','threads') and s.get('enabled', True)]
    disabled=set()
    registry_path=Path('registry/collectors.json')
    if registry_path.exists():
        from registry import validate
        registry=validate(json.loads(registry_path.read_text()))
        disabled={c['platform'] for c in registry['collectors'] if not c['enabled']}
        sources=[s for s in sources if s['platform'] not in disabled]
    posts, records, probes = {}, [], []
    paused=set()
    for source in sources:
        started = time.monotonic()
        if source['platform'] in paused:
            items,attempts=[],[{'tool':'platform_budget','status':'deferred_rate_limit','posts':0}]
        else:
            items, attempts = await collect(source, 5)
            if attempts[-1]['status']=='http_429':paused.add(source['platform'])
        for item in items:
            posts[item['canonical_key']] = item
        record = {'source_id':source['id'],'url':source['url'],'platform':source['platform'],
                  'status':attempts[-1]['status'],'posts':len(items),'attempts':attempts,
                  'seconds':round(time.monotonic()-started,2)}
        records.append(record)
        print(json.dumps(record), flush=True)
        # Independently verify browser compatibility when the primary succeeded;
        # never retry another engine after a rate limit or explicit access block.
        if source['platform']=='threads' and items and len(attempts)==1:
            try:
                _, attempt = await threads(source, 5, 'threads-crawler')
            except Exception as error:
                attempt = diagnostic('threads-crawler',error_code(error),[])
            probes.append({'source_id':source['id'], **attempt})
        await asyncio.sleep(1)
    # Bounded installation/live checks for the remaining libraries, from the
    # existing catalog only. Full collection uses the normal sharded pipeline.
    for platform in ('instagram','facebook'):
        if platform in disabled:continue
        selected=[s for s in catalog['sources'] if s['platform']==platform and s.get('enabled',True)][:2]
        for source in selected:
            try:items,attempt=await library(source,5)
            except Exception as error:
                tool='instaloader' if platform=='instagram' else 'facebook-scraper'
                items,attempt=[],diagnostic(tool,error_code(error),[])
            probes.append({'source_id':source['id'],'url':source['url'],**attempt})
            for item in items:posts[item['canonical_key']]=item
            print(json.dumps(probes[-1]),flush=True)
            if attempt['status'] in ('http_429','challenge_required'):break
            await asyncio.sleep(2)
    report = {'run_id':os.environ['GITHUB_RUN_ID'],'updated_at':now(),'sources':records,
              'additional_tool_checks':probes,'total_posts':len(posts),
              'outcomes':dict(collections.Counter(r['status'] for r in records))}
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'posts.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in posts.values()),encoding='utf-8')
    import sys
    sys.path.insert(0,str(Path('ops/local-storage').resolve()))
    from storage import pack
    (root/'media').mkdir(exist_ok=True)
    pack(root/'posts.jsonl',root/'media',root/'package.zip')
    print(json.dumps({'total_posts':len(posts),'outcomes':report['outcomes']}))


if __name__=='__main__':
    asyncio.run(main())
