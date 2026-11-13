"""Bounded hourly collection of the 17 verified official catalog channels."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import time
from run import feeds, telegram, error_code, now
from registry import validate


def selected_sources(catalog, config):
    wanted = {i for agency in config['agencies'] for i in agency['alternatives']}
    sources = [s for s in catalog['sources'] if s['id'] in wanted and s.get('enabled', True)]
    if len(sources) != len(wanted) or len(sources) != 17:
        raise ValueError('hourly_catalog_scope_changed')
    if any(s['platform'] not in ('telegram', 'youtube', 'facebook') for s in sources):
        raise ValueError('unsupported_hourly_platform')
    return sources


async def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('Hourly collection requires GitHub-hosted Actions')
    from tinyfish_fetch import TinyFetch
    tinyfish = TinyFetch()
    root = Path('collection/dsns-hourly'); root.mkdir(parents=True, exist_ok=True)
    base = Path(__file__).parent
    catalog = json.loads((base/'sources.json').read_text(encoding='utf-8'))
    config = json.loads((base/'dsns-official-sources.json').read_text(encoding='utf-8'))
    registry = validate(json.loads(Path('registry/collectors.json').read_text(encoding='utf-8')))
    disabled = {r['platform'] for r in registry['collectors'] if not r['enabled']}
    sources = selected_sources(catalog, config)
    limit = 100  # One public head in this lane; resumable history runs separately.
    gate = asyncio.Semaphore(2); results = []; posts = {}
    started = time.monotonic()
    from playwright.async_api import async_playwright
    from browser_public import collect
    async with async_playwright() as pw:
        executable = shutil.which('google-chrome') or shutil.which('chromium')
        if not executable: raise RuntimeError('Hosted runner browser missing')
        browser = await pw.chromium.launch(headless=True, executable_path=executable)
        async def one(source):
            async with gate:
                items = []; status = 'disabled_by_registry'; begin = time.monotonic()
                try:
                    if source['platform'] not in disabled:
                        if source['platform'] == 'facebook':
                            items = await asyncio.wait_for(collect(browser, source, limit), 35)
                        else:
                            fn = telegram if source['platform'] == 'telegram' else feeds
                            items = await asyncio.wait_for(asyncio.to_thread(fn, source, limit), 30)
                        status = 'ok' if items else 'no_public_posts'
                except Exception as error: status = error_code(error)
                items,status,attempts = await tinyfish.recover(source,limit,items,status,[])
                for item in items: posts[item['canonical_key']] = item
                results.append({'source_id':source['id'], 'name':source['name'], 'url':source['url'],
                    'platform':source['platform'], 'status':status, 'posts':len(items), 'attempts':attempts,
                    'latest_published_at':max((p['published_at'] for p in items if p.get('published_at')), default=None),
                    'canonical_keys':sorted({p['canonical_key'] for p in items}),
                    'seconds':round(time.monotonic()-begin, 2)})
        try: await asyncio.gather(*(one(s) for s in sources))
        finally: await browser.close()
    report = {'format':'argus-dsns-hourly-v1', 'run_id':os.environ['GITHUB_RUN_ID'], 'updated_at':now(),
              'sources':results, 'unique_posts':len(posts), 'wall_seconds':round(time.monotonic()-started, 2),
              'scope':'17 verified official DSNS catalog channels', 'posts_per_source_limit':limit}
    from dsns_coverage import coverage
    report['dsns_coverage'] = coverage(results, list(posts.values()), report['updated_at'])
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (root/'posts.jsonl').write_text(''.join(json.dumps(p, ensure_ascii=False)+'\n' for p in posts.values()), encoding='utf-8')
    print(json.dumps({'sources':len(results), 'successful_sources':sum(r['status']=='ok' for r in results),
                      'posts':len(posts), 'seconds':report['wall_seconds']}))
    if any(r['status']=='collection_failed' for r in results): raise RuntimeError('Unexpected collection failure')


if __name__ == '__main__': asyncio.run(main())
