"""Exercise each adapter on a distinct public catalog source and report counts only."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

from cloud_social import library, require_cloud
from embed_social import adapter as embed
from media_metadata import extract
from registry import validate
from remaining_social import adapter
from run import source_valid


async def main():
    require_cloud()
    catalog = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
    sources = {s['url']: s for s in catalog['sources'] if s.get('enabled', True) and source_valid(s)}
    registry = validate(json.loads(Path('registry/collectors.json').read_text()))
    enabled = {c['platform'] for c in registry['collectors'] if c['enabled']}
    regional = [s['url'] for s in catalog['sources'] if s['platform'] == 'facebook'
                and ' '.join(s['name'].lower().split()).startswith(('головне управління дснс', 'гу дснс'))]
    facebook_pages = ['https://www.facebook.com/fixture220', *regional]
    checks = [('fbgql', url, adapter) for url in dict.fromkeys(facebook_pages)] + [
        ('facebook-scraper', 'https://www.facebook.com/fixture190', library),
        ('instaloader', 'https://www.instagram.com/fixture549', library),
        ('gallery-dl', 'https://www.instagram.com/fixture578', embed),
        ('TikTok-Api', 'https://www.tiktok.com/@fixture1107', adapter),
        ('yt-dlp', 'https://www.youtube.com/@fixture1674', extract),
        ('yt-dlp', 'https://www.youtube.com/@' + quote('СпеціальнийавіаційнийзагінОРСЦ'), extract),
        ('snscrape', 'https://fixture.invalid/source/1763', adapter),
    ]
    paused = set()
    reports = []
    records = []
    for tool, url, read in checks:
        source = sources.get(url)
        report = {'tool': tool, 'posts': 0}
        if not source:
            report['status'] = 'catalog_source_unavailable'
        elif source['platform'] not in enabled or (tool == 'yt-dlp' and not registry['execution']['media_fallback']):
            report['status'] = 'disabled_by_registry'
        elif source['platform'] in paused:
            report['status'] = 'deferred_rate_limit'
        else:
            if read in (adapter, embed):
                items, report = await read(source, 5, tool)
            else:
                items, report = await read(source, 5)
            records.extend({'tool': tool, 'post': post} for post in items)
            if report['status'] in ('http_429', 'rate_limited'):
                paused.add(source['platform'])
        reports.append({**report, 'url': url, 'name': source['name'] if source else ''})
        print(json.dumps(reports[-1]), flush=True)
    out = Path('collection/catalog-tools')
    out.mkdir(parents=True, exist_ok=True)
    (out/'report.json').write_text(json.dumps({'run_id': os.environ['GITHUB_RUN_ID'],
        'limit_per_source': 5, 'sources': reports}, indent=2), encoding='utf-8')
    (out/'posts.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in records), encoding='utf-8')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write('| Tool | Source | Result | Posts |\n|---|---|---|---:|\n')
        for row in reports:
            summary.write(f"| {row['tool']} | {row['url']} | {row['status']} | {row['posts']} |\n")
    if any(r['status'] in {'adapter_failed', 'worker_failed', 'invalid_output', 'catalog_source_unavailable'}
           or r.get('error_type') in {'ModuleNotFoundError', 'ImportError', 'TypeError', 'AttributeError'}
           for r in reports):
        raise RuntimeError('Collector installation or adapter failure')


if __name__ == '__main__':
    asyncio.run(main())
