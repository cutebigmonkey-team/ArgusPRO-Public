"""Adapters for public profiles, identified permalinks and optional hosted actors."""
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from browser_public import page_error
from candidate_feeds import tiktok_target
from cloud_social import STOP, process, require_cloud, threads_rows, threads_target, tool_root
from public_embeds import timestamp
from remaining_http import PublicHTTP, StopCollection
from run import error_code, post, source_valid, url_ok
from social_links import identity

PINS = json.loads(Path(__file__).with_name('source-adapters.json').read_text())
WALL = STOP | {'unexpected_destination', 'invalid_source', 'deferred_rate_limit', 'disabled_by_registry'}
X = re.compile(r'/([A-Za-z0-9_]{1,15})(?:/status/(\d{1,22}))?/?')
ACTORS = {'socialdataapi-reels': 'ZOChpvBhFGxNLydzz',
          'logiover-threads': 'logiover~threads-scraper'}


def http_json(url, *, body=None, token=None, params=None, accepted=(200, 201)):
    # Only fixed runner endpoints and the two fixed Apify actor paths call this.
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with requests.Session() as session:
        session.trust_env = False
        with session.request('POST' if body is not None else 'GET', url, headers=headers, json=body,
                             params=params, allow_redirects=False, stream=True,
                             timeout=(4, 65 if token else 18)) as response:
            if response.status_code not in accepted:
                if response.status_code in (429,503):
                    from datetime import datetime, timezone
                    from source_health import note_retry_after
                    note_retry_after(response.headers.get('Retry-After'),datetime.now(timezone.utc))
                raise ValueError('http_' + str(response.status_code))
            data = bytearray()
            for block in response.iter_content(65536):
                data.extend(block)
                if len(data) > 4 * 1024**2:
                    raise ValueError('response_too_large')
            return json.loads(data)


def pravaha(source, limit):
    from candidate_audit import count
    owner, sid = X.fullmatch(urlsplit(source['url']).path).groups()
    route = 'tweet/' + sid if sid else 'timeline/' + owner
    data = http_json('http://127.0.0.1:4000/api/' + route)
    if data.get('ok') is not True:
        raise ValueError('upstream_rejected')
    accepted = {}
    tweets=[data.get('tweet')] if sid else data.get('tweets', [])
    count('raw_input',len(tweets))
    for tweet in tweets:
        if not isinstance(tweet, dict) or str(tweet.get('authorHandle')).lower() != owner.lower():
            count('source_owner_mismatch')
            continue
        key = str(tweet.get('id', ''))
        if not re.fullmatch(r'\d{1,22}', key) or (sid and sid != key):
            count('invalid_post_identity')
            continue
        item = post(source, 'x:' + key, f'https://x.com/{owner}/status/{key}', tweet.get('text') or '',
                    tweet.get('createdAt'), [m.get('url') for m in tweet.get('media', [])])
        from engagement_metrics import retain_counters
        # Pinned Pravaha defaults absent counters to 0 (syndication views are
        # always 0). Only its positive observations are distinguishable from
        # those placeholders; the native X API retains verified zero separately.
        retain_counters(item,{name:tweet[field] for name,field in
            (('likes','likes'),('comments','replies'),('shares','retweets'),('views','views'))
            if type(tweet.get(field)) is int and tweet[field]>0})
        if item['text'].strip() or item['media_urls']:
            if key in accepted:
                count('duplicate_identity')
            accepted[key] = item
        else:
            count('empty_content')
    return list(accepted.values())


async def threads_cli(source, limit):
    owner, code = threads_target(source)
    args = ['post', source['url']] if code else ['profile', owner, '--posts']
    command = [str(tool_root() / 'tamnd-th'), *args, '-o', 'jsonl', '-n', str(limit), '--retries', '0', '--no-cache']
    with tempfile.TemporaryDirectory(prefix='argus-th-') as directory:
        work = Path(directory)
        rc, status, output = await process(command, work, work/'unused', timeout=40)
        if status or rc:
            raise ValueError(status or {3:'unavailable', 4:'login_required', 5:'http_429', 6:'network_error'}.get(rc, 'worker_failed'))
        rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines() if line.strip()]
        from candidate_audit import count
        count('raw_input',len(rows))
    return threads_rows(source, rows, limit, 'threads-cli', PINS['threads-cli']['commit'])


def detail_key(platform, url):
    if not source_valid({'platform': platform, 'url': url}):
        return None
    if platform == 'instagram':
        found = identity(url)
        return found and found['canonical_key']
    if platform == 'tiktok':
        match = tiktok_target(url)
        if match and match[2]:
            return (match[1].lower(), match[2])
    if platform == 'threads':
        match = re.fullmatch(r'/@([\w.]+)/post/([\w-]+)/?', urlsplit(url).path, re.ASCII)
        if match:
            return (match[1].lower(), match[2])
    return None


def social_fetch(source):
    platform, url = source['platform'], source['url']
    html = PublicHTTP(platform, budget=3).request(url)
    if status := page_error(url, html):
        raise ValueError(status)
    soup = BeautifulSoup(html, 'html.parser')
    meta = {tag.get('property'): tag.get('content', '').strip() for tag in soup.select('meta[property][content]')}
    canonical = meta.get('og:url', '')
    target = detail_key(platform, url)
    if not target or detail_key(platform, canonical) != target:
        return []
    key = target if platform == 'instagram' else platform + ':' + target[1]
    body = meta.get('og:description', '')
    media = [meta.get('og:video'), meta.get('og:image')] if platform != 'threads' else []
    if not body and not any(url_ok(link) for link in media):
        return []
    item = post(source, key, canonical, body, meta.get('article:published_time'), media)
    item['text_may_be_truncated'] = True
    return [item]


def single_reel_rows(source, data):
    target = identity(source['url'])
    for row in data:
        found = identity(row.get('url'))
        if (row.get('success') is not True or not found or found['canonical_key'] != target['canonical_key']
                or 'instagram:' + str(row.get('shortcode')) != target['canonical_key']):
            continue
        item = post(source, found['canonical_key'], target['url'], row.get('captionText') or '',
                    timestamp(row.get('takenAtIso') or row.get('takenAt')), [row.get('videoUrlBest'), row.get('thumbnailUrl')])
        if not item['text'] and not item['media_urls']:
            continue
        comments = {}
        for node in (row.get('commentsExtracted') or [])[:20]:
            if isinstance(node, dict) and isinstance(node.get('text'), str) and node['text'].strip():
                cid = str(node.get('id') or node.get('pk') or '')
                if cid.isdigit():
                    comments[cid] = {'id': cid, 'text': node['text'][:1000], 'author': str(node.get('username') or '')[:64]}
        if comments:
            item['instagram_comments'] = list(comments.values())
        item['engagement'] = {k:row[k] for k in ('likeCount','commentCount','playCount') if type(row.get(k)) is int}
        return [item]
    return []


def apify(tool, source, limit, token):
    limit = min(5, limit)
    if tool == 'logiover-threads':
        owner, code = threads_target(source)
        body = {'postUrls': [f'https://www.threads.com/@{owner}/post/{code}']} if code else {'usernames': [owner]}
        body.update(maxResults=limit, includeReplies=False, expandFromProfiles=False, expandFromSearch=False)
    else:
        body = {'urls': [identity(source['url'])['url']]}
    data = http_json('https://api.apify.com/v2/acts/' + ACTORS[tool] + '/run-sync-get-dataset-items',
                     body=body, token=token, params={'timeout': 60, 'limit': limit})
    if not isinstance(data, list):
        raise ValueError('invalid_output')
    if tool == 'socialdataapi-reels':
        return single_reel_rows(source, data)
    rows = []
    for raw in data:
        username = (raw.get('author') or {}).get('username')
        if raw.get('isReply') or not username:
            continue
        rows.append({'permalink': raw.get('url'), 'username': username, 'text': raw.get('text'),
                     'timestamp': raw.get('createdAt'), 'media_urls': [m.get('url') for m in raw.get('media') or []],
                     **{k:raw[v] for k, v in (('like_count','likeCount'), ('reply_count','replyCount'),
                                              ('repost_count','repostCount')) if type(raw.get(v)) is int}})
    return threads_rows(source, rows, limit, tool, PINS[tool]['commit'])


async def read(tool, source, limit, token=None):
    require_cloud()
    from collector_preflight import unavailable_reason
    if reason:=unavailable_reason(tool):
        return [],{'tool':tool,'status':'tool_unavailable','reason':reason,'posts':0}
    import candidate_audit
    from collections import Counter
    from source_health import retry_hints, instant
    hints=[]
    hint_token=retry_hints.set(hints)
    counts=Counter()
    context_token=candidate_audit.current.set(counts)
    provenance = {'actor_id': ACTORS[tool]} if tool in ACTORS else {'upstream_commit': PINS[tool]['commit']}
    try:
        if tool == 'threads-cli':
            items = await threads_cli(source, limit)
        elif tool == 'pravaha':
            items = await asyncio.to_thread(pravaha, source, limit)
        elif tool == 'social-fetch':
            items = await asyncio.to_thread(social_fetch, source)
        elif token and tool in ACTORS:
            items = await asyncio.to_thread(apify, tool, source, limit, token)
        else:
            raise ValueError('not_configured')
        for item in items:
            item['capture_mode'] = tool
            if tool in ACTORS:
                item['actor_id'] = ACTORS[tool]
            else:
                item['collector_commit'] = PINS[tool]['commit']
        status = 'ok' if items else 'no_public_posts'
    except StopCollection as error:
        items, status = [], error.status
    except (requests.Timeout, TimeoutError):
        items, status = [], 'timeout'
    except (requests.ConnectionError, FileNotFoundError):
        items, status = [], 'tool_unavailable'
    except Exception as error:
        items, status = [], error_code(error)
    finally:
        candidate_audit.current.reset(context_token)
        retry_hints.reset(hint_token)
    audit={'retry_after':max(hints,key=instant)} if hints else {}
    if 'raw_input' in counts:
        audit.update(raw_rows=counts.pop('raw_input'),rejected_rows=dict(counts))
    return items, {'tool': tool, **provenance, **audit, 'posts': len(items), 'status': status}


def primary(source):
    platform, path = source['platform'], urlsplit(source['url']).path
    from x_api import RESERVED
    match = X.fullmatch(path) if platform == 'x' else None
    if match and match[1].lower() not in RESERVED:
        return 'pravaha'
    if platform == 'threads' and re.fullmatch(r'/@[A-Za-z0-9_.]{1,64}(?:/post/[A-Za-z0-9_-]{1,64})?/?', path):
        return 'threads-cli'
    return None


async def prime_x_sources(sources, limit, pause):
    """Read all eligible X timelines before an empty one can start a fallback."""
    prepared = {}
    for source in sources:
        if not source_valid(source) or primary(source) != 'pravaha':
            continue
        from collector_preflight import unavailable_reason
        if prepared and not unavailable_reason('pravaha'):
            await asyncio.sleep(pause)
        started = time.monotonic()
        try:
            items, attempt = await read('pravaha', source, limit)
        except Exception as error:
            items, attempt = [], {'tool':'pravaha','status':error_code(error),'posts':0}
        prepared[source['id']] = (items, {**attempt, 'primary_seconds': round(time.monotonic() - started, 2)})
        if attempt['status'] == 'http_429':
            return prepared, True
    return prepared, False


async def collect(browser, source, limit, media_fallback, legacy, token, primary_result=None):
    attempts = []
    first = primary(source)
    if first:
        if primary_result is None:
            primary_result = await read(first, source, min(limit, 100))
        items, attempt = primary_result
        attempts.append(attempt)
        from candidate_audit import windowed
        raw_count=len(items)
        items=windowed(items)
        if raw_count!=len(items):
            attempt['raw_rows']=max(raw_count,attempt.get('raw_rows',0))
            attempt['outside_window']=raw_count-len(items)
            attempt['posts']=len(items)
            if not items and attempt['status']=='ok':attempt['status']='stale_feed_only'
        if items or attempt['status'] in WALL:
            return items, attempt['status'], attempts
    items, status, earlier = await legacy(browser, source, limit, media_fallback)
    attempts.extend(earlier)
    if items or status in WALL or any(a['status'] in WALL or a['status'].startswith('robots_') for a in earlier):
        return items, status, attempts
    platform, url = source['platform'], source['url']
    if detail_key(platform, url):
        items, attempt = await read('social-fetch', source, 1)
        attempts.append(attempt)
        if items or attempt['status'] in WALL:
            return items, attempt['status'], attempts
    actor = None
    if first == 'threads-cli' and os.getenv('ARGUS_LOGIOVER_ENABLED') == 'true':
        actor = 'logiover-threads'
    if platform == 'instagram' and (found := identity(url)) and '/reel/' in found['url'] and os.getenv('ARGUS_SOCIALDATAAPI_REELS_ENABLED') == 'true':
        actor = 'socialdataapi-reels'
    if actor:
        items, attempt = await read(actor, source, min(limit, 5), token)
        attempts.append(attempt)
        return items, attempt['status'], attempts
    return items, attempts[-1]['status'] if attempts else status, attempts


async def supplement_threads(source, limit, items, attempts):
    """Use one public profile page to fill the remaining feed budget."""
    from cloud_social import threads

    if (len(items) >= limit or not re.fullmatch(r'/@[A-Za-z0-9_.]{1,64}/?', urlsplit(source['url']).path)
            or any(a['tool'] == 'threads-crawler' or a['status'] in WALL or a['status'].startswith('robots_')
                   for a in attempts)):
        return
    try:
        rows, attempt = await threads(source, min(limit, 100), 'threads-crawler')
    except Exception as error:
        rows, attempt = [], {'tool': 'threads-crawler', 'status': error_code(error), 'posts': 0}
    existing = {row['canonical_key'] for row in items}
    added = [row for row in rows if row['canonical_key'] not in existing][:limit - len(items)]
    items.extend(added)
    attempts.append({**attempt, 'added_posts': len(added)})


async def enrich_detail(source, items, attempts, token):
    """Try one identified permalink discovered on a catalog profile."""
    platform = source['platform']
    if platform not in ('instagram', 'threads', 'tiktok') or detail_key(platform, source['url']):
        return
    for item in items:
        url = item.get('post_url')
        target = detail_key(platform, url)
        if not target:
            continue
        key = target if platform == 'instagram' else platform + ':' + target[1]
        if item.get('canonical_key') != key or (item.get('text') and item.get('media_urls')):
            continue
        detail = {**source, 'url': url}
        rows, attempt = await read('social-fetch', detail, 1)
        attempts.append(attempt)
        if (not rows and attempt['status'] not in WALL and platform == 'instagram'
                and '/reel/' in identity(url)['url'] and os.getenv('ARGUS_SOCIALDATAAPI_REELS_ENABLED') == 'true'):
            rows, attempt = await read('socialdataapi-reels', detail, 1, token)
            attempts.append(attempt)
        if rows and rows[0]['canonical_key'] == key:
            row = rows[0]
            if not item.get('text') and row['text']:
                item['text'] = row['text']
                item['text_may_be_truncated'] = row.get('text_may_be_truncated', False)
            item['media_urls'] = list(dict.fromkeys(item.get('media_urls', []) + row['media_urls']))[:12]
            item['published_at'] = item.get('published_at') or row['published_at']
            if row.get('instagram_comments'):
                item['instagram_comments'] = row['instagram_comments']
        break


def cobalt_key(item):
    from candidate_media import video_id
    url = item.get('post_url')
    platform = item.get('platform')
    if not source_valid({'platform': platform, 'url': url}) or urlsplit(url).scheme != 'https':
        return None
    if platform == 'instagram':
        found = identity(url)
        key = found and found['canonical_key']
    elif platform == 'vk':
        match = re.fullmatch(r'/(?:clip|video)(-?\d+_\d+)/?', urlsplit(url).path)
        key = match and 'vk:' + match[1]
    elif platform == 'ok':
        match = re.fullmatch(r'/video/(\d+)/?', urlsplit(url).path)
        key = match and 'ok:' + match[1]
    else:
        return video_id(item)
    return key if key and key == item['canonical_key'] else None


def cobalt_urls(url):
    from run import safe_url
    data = http_json('http://127.0.0.1:9000/', body={'url': url, 'videoQuality': '720'}, accepted=(200, 400))
    status = data.get('status')
    if status == 'redirect':
        urls = [data.get('url')]
    elif status == 'picker':
        urls = [part.get('url') for part in data.get('picker', [])[:12]]
    elif status == 'tunnel':
        # The runner and its tunnels expire when the job ends.
        return [], 'temporary_tunnel'
    elif status == 'error':
        code = (data.get('error') or {}).get('code', '')
        return [], code if isinstance(code, str) and re.fullmatch(r'error\.[a-z0-9_.]{1,80}', code) else 'media_unavailable'
    else:
        raise ValueError('invalid_output')
    result = []
    for value in urls:
        if not url_ok(value) or urlsplit(value).scheme != 'https':
            continue
        try:
            safe_url(value)
        except (OSError, ValueError):
            continue
        result.append(value)
    return list(dict.fromkeys(result)), 'ok' if result else 'no_public_media'


async def enrich_media(items, attempts):
    for item in items:
        if item.get('media_urls') or not cobalt_key(item):
            continue
        try:
            urls, status = await asyncio.to_thread(cobalt_urls, item['post_url'])
        except requests.ConnectionError:
            urls, status = [], 'tool_unavailable'
        except Exception as error:
            urls, status = [], error_code(error)
        if urls:
            item['media_urls'] = urls
        attempts.append({'tool':'scrapedog-cobalt', 'upstream_commit':PINS['scrapedog-cobalt']['commit'],
                         'status':status, 'posts':0, 'media_count':len(urls)})
        break
