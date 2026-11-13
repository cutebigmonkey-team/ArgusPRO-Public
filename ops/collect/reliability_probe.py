"""Compare bounded anonymous controls across two checkouts without archive credentials."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys


async def probe(checkout, output):
    sys.path.insert(0, str(checkout.resolve()/'ops/collect'))
    from cloud_social import require_cloud
    from playwright.async_api import async_playwright
    from run import PLATFORMS, collect_source, date, error_code, source_valid, url_ok
    require_cloud()
    catalog = json.loads((checkout/'ops/collect/sources.json').read_text(encoding='utf-8'))['sources']
    pins = {'facebook':'https://www.facebook.com/fixture220', 'telegram':'https://t.me/fixture110',
            'youtube':'https://www.youtube.com/@fixture1674', 'threads':'https://www.threads.net/@dsns_ukraine'}
    results = {}
    gate = asyncio.Semaphore(3)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=shutil.which('google-chrome'))
        async def one(platform):
            choices = [s for s in catalog if s.get('enabled',True) and s['platform']==platform and source_valid(s)]
            source = next((s for s in choices if s['url']==pins.get(platform)), choices[0] if choices else None)
            if platform == 'reddit' and not source:
                source = {'id':'public-control','platform':'reddit','url':'https://www.reddit.com/r/Python'}
            if not source:
                results[platform]={'status':'no_configured_sources','keys':[]}
                return
            async with gate:
                try:
                    rows, status, attempts = await asyncio.wait_for(collect_source(browser,source,5),100)
                except (TimeoutError, ValueError, OSError) as exc:
                    rows,status,attempts=[],error_code(exc),[]
            keys = set()
            for row in rows:
                key=row['canonical_key']
                if (not key.startswith(platform+':') or not url_ok(row['post_url']) or
                    not (row['text'].strip() or row['media_urls']) or
                    (row['published_at'] and not date(row['published_at']))):
                    raise ValueError('invalid_control_record')
                keys.add(hashlib.sha256(key.encode()).hexdigest())
            if len(keys)!=len(rows):
                raise ValueError('duplicate_control_records')
            results[platform]={'source':source['url'], 'status':status,'keys':sorted(keys),
                'confirmed_dates':sum(bool(r['published_at']) and not r.get('published_at_is_estimated',False) for r in rows),
                'full_texts':sum(bool(r['text']) and r.get('text_may_be_truncated') is False for r in rows),
                'attempts':[{'tool':a['tool'],'status':a.get('status')} for a in attempts]}
        await asyncio.gather(*(one(p) for p in PLATFORMS))
        await browser.close()
    output.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    if not any(r['keys'] for r in results.values()):
        raise ValueError('no_verified_control_posts')


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('checkout', type=Path)
    p.add_argument('output', type=Path)
    args=p.parse_args()
    asyncio.run(probe(args.checkout,args.output))
