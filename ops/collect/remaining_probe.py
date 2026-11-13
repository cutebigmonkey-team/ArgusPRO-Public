"""Bounded live verification on enabled sources from the actual Argus catalog."""
import asyncio
import collections
import json
import os
from pathlib import Path
import time
from remaining_social import collect, require_cloud
from run import now, source_valid


async def main():
    require_cloud()
    root = Path('collection/remaining-probe'); root.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(Path('ops/collect/sources.json').read_text(encoding='utf-8'))
    from registry import validate
    registry = validate(json.loads(Path('registry/collectors.json').read_text()))
    disabled = {c['platform'] for c in registry['collectors'] if not c['enabled']}
    records, posts = [], {}
    platforms = os.environ.get('ARGUS_PROBE_PLATFORMS','facebook,instagram,vk,ok').split(',')
    if not platforms or set(platforms) - {'facebook','instagram','vk','ok'}:
        raise ValueError('invalid_probe_platforms')
    for platform in dict.fromkeys(platforms):
        if platform in disabled:
            continue
        candidates = [s for s in catalog['sources'] if s['platform']==platform and s.get('enabled',True) and source_valid(s)]
        # Include central/regional DSNS pages, rather than judging Facebook
        # coverage solely by the first alphabetically sorted local stations.
        if platform == 'facebook':
            preferred = ('https://www.facebook.com/fixture220','https://www.facebook.com/fixture293')
            candidates.sort(key=lambda s: (0 if s['url'] in preferred else 1, s['url']))
        selected = candidates[:3]
        for source in selected:
            start = time.monotonic()
            items, attempts = await collect(source, 5)
            for item in items:
                posts[item['canonical_key']] = item
            record = {'source_id':source['id'], 'url':source['url'], 'platform':platform,
                      'posts':len(items), 'status':attempts[-1]['status'], 'attempts':attempts,
                      'seconds':round(time.monotonic()-start,2)}
            records.append(record)
            print(json.dumps(record), flush=True)
            if record['status'] in ('http_429','challenge_required'):
                break
            await asyncio.sleep(2)
    report = {'run_id':os.environ['GITHUB_RUN_ID'], 'updated_at':now(), 'sources':records,
              'requested_platforms':platforms,
              'total_posts':len(posts), 'outcomes':dict(collections.Counter(r['status'] for r in records))}
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'posts.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in posts.values()),encoding='utf-8')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:
            output.write('posts='+str(len(posts))+'\n')
    import sys
    sys.path.insert(0,str(Path('ops/local-storage').resolve()))
    from storage import pack
    (root/'media').mkdir(exist_ok=True)
    pack(root/'posts.jsonl',root/'media',root/'package.zip')


if __name__ == '__main__':
    asyncio.run(main())
