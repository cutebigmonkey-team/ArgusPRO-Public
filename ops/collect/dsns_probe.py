"""Cloud-only verification of existing catalog alternatives to 12 DSNS sites."""
from private_assets import private_asset_path
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import time
from run import fetch, telegram, error_code, now, collect_source
from youtube_feed import resolve, parse


async def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('Probe requires GitHub-hosted Actions')
    root = Path('collection/dsns-probe'); root.mkdir(parents=True, exist_ok=True)
    config = json.loads(private_asset_path('dsns-official-sources.json').read_text(encoding='utf-8'))
    catalog = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
    sources = {s['id']: s for s in catalog['sources']}
    ids = sorted({i for agency in config['agencies'] for i in agency['alternatives']})
    results = []; posts = {}; gate = asyncio.Semaphore(2)
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        async def one(identity):
            async with gate:
                source = sources[identity]
                if not source.get('enabled', True):
                    results.append({'source_id': identity, 'status': 'disabled', 'posts': 0}); return
                row = {k: source[k] for k in ('name', 'url', 'platform')}
                row.update(source_id=identity, references=source.get('references', []))
                started = time.monotonic(); items = []
                try:
                    if source['platform'] == 'youtube':
                        channel = await asyncio.to_thread(resolve, source, fetch, False)
                        if resolve(source, fetch) != channel: raise ValueError('pinned_channel_owner_changed')
                        row.update(channel_id=channel, feed_url='https://www.youtube.com/feeds/videos.xml?channel_id=' + channel)
                        body, _ = await asyncio.to_thread(fetch, row['feed_url'])
                        import feedparser
                        parsed = feedparser.parse(body)
                        row['feed_title'] = parsed.feed.get('title', '')
                        row['entry_identities'] = [{k: entry.get(k) for k in ('yt_channelid', 'yt_videoid', 'link')} for entry in parsed.entries[:5]]
                        items = parse(source, body, channel, 5)
                    elif source['platform'] == 'telegram':
                        items = await asyncio.to_thread(telegram, source, 5)
                    else:
                        items, status, attempts = await collect_source(browser, source, 5)
                        row.update(status=status, attempts=attempts)
                    row.setdefault('status', 'ok' if items else 'no_public_posts')
                except Exception as error:
                    row['status'] = error_code(error)
                row.update(posts=len(items), seconds=round(time.monotonic()-started, 2))
                stamps = [p['published_at'] for p in items if p.get('published_at')]
                row['latest_published_at'] = max(stamps, default=None)
                row['latest_age_days'] = round((dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(max(stamps))).total_seconds()/86400, 2) if stamps else None
                for item in items: posts[item['canonical_key']] = item
                results.append(row); print(json.dumps(row, ensure_ascii=True), flush=True)
        await asyncio.gather(*(one(i) for i in ids))
        await browser.close()
    by_id = {r['source_id']: r for r in results}
    coverage = []
    for agency in config['agencies']:
        working = [i for i in agency['alternatives'] if by_id[i]['posts']]
        recent = [i for i in working if by_id[i]['latest_age_days'] is not None and 0 <= by_id[i]['latest_age_days'] <= 30]
        undated = any(by_id[i]['latest_age_days'] is None for i in working)
        coverage.append({**agency, 'working_alternatives': working, 'recent_alternatives': recent,
                         'status': 'recent_alternative' if recent else 'undated_alternative' if undated else 'historical_alternative' if working else 'uncovered'})
    report = {'schema': 'argus-dsns-official-probe-v1', 'observed_at': now(), 'sources': results,
              'agencies': coverage, 'unique_posts': len(posts), 'covered': sum(bool(r['working_alternatives']) for r in coverage),
              'recent_covered': sum(bool(r['recent_alternatives']) for r in coverage)}
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (root/'posts.jsonl').write_text(''.join(json.dumps(p, ensure_ascii=False)+'\n' for p in posts.values()), encoding='utf-8')
    if any(r['status']=='collection_failed' for r in results): raise RuntimeError('Unexpected collector failure')


if __name__ == '__main__': asyncio.run(main())
