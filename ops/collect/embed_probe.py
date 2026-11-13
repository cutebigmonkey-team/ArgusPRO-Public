"""Probe the new engines on the enabled catalog, without third-party RSS hosts."""
import asyncio
import collections
import json
import os
from pathlib import Path
import time
from embed_social import collect, adapter, MANIFEST, require_cloud
from run import now, source_valid


async def main():
    require_cloud()
    root = Path('collection/embed-probe'); root.mkdir(parents=True,exist_ok=True)
    catalog = json.loads(Path('ops/collect/sources.json').read_text(encoding='utf-8'))
    from registry import validate
    registry = validate(json.loads(Path('registry/collectors.json').read_text()))
    disabled = {c['platform'] for c in registry['collectors'] if not c['enabled']}
    posts, records = {}, []
    for platform in ('instagram','facebook'):
        if platform in disabled: continue
        candidates = [s for s in catalog['sources'] if s['platform']==platform and s.get('enabled',True) and source_valid(s)]
        preferred = ('dsns.gov.ua','dsns_ukraine','lvivdsns','dsns_lviv','dsns_dnipro')
        candidates.sort(key=lambda s:(0 if s['url'].rstrip('/').rsplit('/',1)[-1] in preferred else 1,s['url']))
        for source in candidates[:3]:
            start = time.monotonic()
            if os.environ.get('ARGUS_PROBE_ENGINE') == 'gallery-dl':
                items, attempt = await adapter(source,5,'gallery-dl')
                attempts = [attempt]
            else:
                items, attempts = await collect(source,5)
            for item in items: posts[item['canonical_key']] = item
            record = {'source_id':source['id'],'url':source['url'],'platform':platform,'posts':len(items),
                      'status':attempts[-1]['status'],'attempts':attempts,'seconds':round(time.monotonic()-start,2)}
            records.append(record); print(json.dumps(record),flush=True)
            if record['status'] in ('http_429','challenge_required'): break
            await asyncio.sleep(2)
    report = {'run_id':os.environ['GITHUB_RUN_ID'],'updated_at':now(),'sources':records,'total_posts':len(posts),
              'outcomes':dict(collections.Counter(r['status'] for r in records)),
              'not_installed':[{'tool':'unsocial',**MANIFEST['unsocial']}]}
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'posts.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in posts.values()),encoding='utf-8')
    with open(os.environ['GITHUB_OUTPUT'],'a') as output: output.write('posts='+str(len(posts))+'\n')
    import sys
    sys.path.insert(0,str(Path('ops/local-storage').resolve()))
    from storage import pack
    (root/'media').mkdir(exist_ok=True)
    pack(root/'posts.jsonl',root/'media',root/'package.zip')


if __name__=='__main__': asyncio.run(main())
