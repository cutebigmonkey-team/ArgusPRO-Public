"""Cloud-only adapters with common post identity and ownership validation."""
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from cloud_social import STOP, process, require_cloud, tool_root, platform_token

MANIFEST = json.loads(Path(__file__).with_name('remaining-upstreams.json').read_text())
CHAINS = {'facebook': ('fbgql', 'skraper', 'socialfetch', 'granary'),
          'instagram': ('skraper', 'socialfetch', 'granary'),
          'vk': ('skraper', 'vkrss'), 'ok': ('skraper',),
          'x': ('snscrape',), 'tiktok': ('TikTok-Api',)}


def owner(url):
    u = urlsplit(url)
    path = u.path.strip('/').lower()
    if path in ('profile.php', 'permalink.php', 'story.php'):
        return parse_qs(u.query).get('id', [''])[0]
    parts = path.split('/')
    return '/'.join(parts[:2]) if parts[0] in ('groups', 'group', 'profile') else parts[0]


def same_owner(expected, actual):
    if not actual:
        return False
    return expected == actual.lower().strip('/').removeprefix('@')


def normalize(source, rows, limit, tool, commit=None, *, rejected=None):
    from run import HOSTS, IDENTITIES, post, plain, social_url, url_ok
    expected = owner(source['url']).removeprefix('@')
    requested = re.search(IDENTITIES[source['platform']], source['url'])
    result = {}
    from collections import Counter
    reasons = rejected if rejected is not None else Counter()
    for row in rows:
        if not isinstance(row, dict):
            reasons['invalid_row']+=1
            continue
        url = row.get('url', '')
        if not isinstance(url,str) or not url.strip():
            reasons['missing_permalink']+=1
            continue
        url=urljoin(source['url'],url)
        if not url_ok(url) or urlsplit(url).hostname not in HOSTS[source['platform']]:
            reasons['invalid_permalink']+=1
            continue
        identity = re.search(IDENTITIES[source['platform']], url)
        if not identity or (requested and requested[1] != identity[1]):
            reasons['unverified_post_identity']+=1
            continue
        # A directly requested post is its own scope. Profile feeds need an
        # author matched to the requested profile from the same response.
        if not requested and not same_owner(expected, row.get('source_owner', '')):
            reasons['source_owner_mismatch']+=1
            continue
        text = plain(row.get('text') or '')
        media = row.get('media') or []
        if not isinstance(media, list):
            media=[]
        media = [m for m in media if isinstance(m, str) and url_ok(m)]
        if not text and not media:
            reasons['no_public_content']+=1
            continue
        stamp = row.get('published')
        if isinstance(stamp, (int, float)):
            try:
                stamp = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat() if stamp > 0 else None
            except (ValueError, OverflowError, OSError):
                stamp = None
        key = source['platform'] + ':' + identity[1]
        item = post(source, key, social_url(url), text, stamp, media)
        from engagement_metrics import retain_counters
        # Only counters explicitly retained by the owning adapter; generic
        # upstream DTOs can manufacture default zero fields.
        retain_counters(item,{'engagement':row.get('engagement',{})})
        item.update(capture_mode=tool, collector_commit=commit or MANIFEST[tool]['commit'])
        if tool == 'fbgql' and str(row.get('facebook_story_id','')).isdigit():
            if identity[1].isdigit() and identity[1]!=str(row['facebook_story_id']):
                reasons['story_id_conflict']+=1
                continue
            item['facebook_story_id'] = str(row['facebook_story_id'])
            if item['published_at']:
                item['facebook_date_source'] = 'story_creation_time'
        if tool == 'skraper' and source['platform'] == 'vk' and item['published_at']:
            item['published_at'] = None
        if key in result:reasons['duplicate_in_response']+=1
        result[key] = item
        if len(result) >= limit:
            break
    return list(result.values())


def diagnostic(tool, status, items):
    return {'tool':tool, 'status':status, 'posts':len(items), 'upstream_commit':MANIFEST[tool]['commit']}


async def adapter(source, limit, tool):
    require_cloud()
    from run import source_valid
    if not source_valid(source) or tool not in CHAINS.get(source['platform'], ()):
        return [], diagnostic(tool, 'invalid_source', [])
    if not 1 <= limit <= 100:
        raise ValueError('invalid_limit')
    if tool == 'vkrss' and not platform_token('ARGUS_VK_TOKEN'):
        return [], diagnostic(tool, 'requires_vk_token', [])
    with tempfile.TemporaryDirectory(prefix='argus-remaining-') as directory:
        work = Path(directory)
        request, output = work/'request.json', work/'result.json'
        request.write_text(json.dumps({'source':source,'limit':limit,'tool':tool,'tools':str(tool_root())}), encoding='utf-8')
        environment = 'short-post-python' if tool in ('TikTok-Api', 'snscrape') else 'remaining-python'
        command = [str(tool_root()/environment/'bin/python'), str(Path(__file__).with_name('remaining_worker.py')),
                   str(request), str(output)]
        # The token is explicitly scoped to the one reserve worker; archive and
        # registry credentials never enter collector subprocesses.
        extra = {'ARGUS_VK_TOKEN':platform_token('ARGUS_VK_TOKEN')} if tool == 'vkrss' else None
        code, status, _ = await process(command, work, output, timeout=55, extra_env=extra)
        if status or code or not output.exists():
            return [], diagnostic(tool, status or 'worker_failed', [])
        data = json.loads(output.read_text())
        from collections import Counter
        rejected=Counter()
        items = normalize(source, data.get('rows', []), limit, tool, rejected=rejected)
        status = data.get('status', 'ok' if items else 'no_public_posts')
        if not items and status == 'ok':
            status = 'unverified_posts'
        attempt = diagnostic(tool, status, items)
        attempt['raw_rows']=len(data.get('rows', []))
        remaining=attempt['raw_rows']-len(items)-sum(rejected.values())
        if remaining:rejected['source_limit']=remaining
        attempt['rejected_rows']=dict(rejected)
        if re.fullmatch(r'[A-Za-z]{1,80}', str(data.get('error_type', ''))):
            attempt['error_type'] = data['error_type']
        if isinstance(data.get('details'), dict):
            attempt['details'] = {k:v for k,v in data['details'].items()
                                  if k in ('response_bytes','raw_posts','verified_posts','post_links','graphql_calls') and type(v) is int}
        return items, attempt


async def collect(source, limit, *, skip=()):
    require_cloud()
    from record_quality import merge_observations
    collected = {}
    attempts = []
    deadline = time.monotonic() + 75
    from source_health import preferred
    for tool in preferred(CHAINS[source['platform']]):
        if tool in skip:
            continue
        started = time.monotonic()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append(diagnostic(tool, 'source_budget', []))
                break
            items, attempt = await asyncio.wait_for(adapter(source, limit, tool), timeout=remaining)
        except TimeoutError:
            attempts.append(diagnostic(tool, 'source_budget', []))
            break
        except Exception:
            items, attempt = [], diagnostic(tool, 'adapter_failed', [])
        attempt['seconds'] = round(time.monotonic()-started,2)
        attempts.append(attempt)
        for item in items:
            key = item['canonical_key']
            collected[key] = merge_observations(collected.get(key), item)
        if len(collected) >= limit or attempt['status'] in STOP:
            break
        await asyncio.sleep(1)
    return list(collected.values())[:limit], attempts
