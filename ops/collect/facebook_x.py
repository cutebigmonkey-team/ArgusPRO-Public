"""Bounded Facebook and X reads on hosted runners, isolated from collector credentials."""
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

from browser_public import facebook_owned
from candidate_feeds import PINS
from cloud_social import process, require_cloud, tool_root
from run import error_code, post, source_valid
from social_links import identity
from x_post_details import x_target


def facebook_command(source, limit):
    url = source['url']
    target = identity(url)
    if target:
        return ['post', target['url']] if target['platform'] == 'facebook' else None
    parsed = urlsplit(url)
    owner = parsed.path.strip('/')
    if owner == 'profile.php':
        owner = parse_qs(parsed.query).get('id', [''])[0]
        if not owner.isdigit():
            return None
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.]{0,75}', owner) and '.php' not in owner:
        return ['feed', owner, '-n', str(limit)]
    return None


def facebook_rows(source, rows, limit):
    requested = identity(source['url'])
    result = {}
    for row in rows:
        found = identity(row.get('url'))
        if not found or found['platform'] != 'facebook' or row.get('tier') != 0:
            continue
        story_id = str(row.get('id') or '')
        link_id = found['canonical_key'].split(':')[1]
        if link_id.isdigit() and story_id != link_id:
            continue
        author = row.get('author') or {}
        if requested:
            if requested['canonical_key'] not in (found['canonical_key'], 'facebook:' + story_id):
                continue
        elif not facebook_owned(source, {'ownerId': author.get('id'), 'ownerUrl': author.get('url')}, found['url']):
            continue
        media = [(a.get('image') or {}).get('uri') for a in row.get('attachments', [])]
        item = post(source, found['canonical_key'], found['url'], (row.get('message') or {}).get('text') or '',
                    row.get('created_at'), media)
        if item['text'].strip() or item['media_urls']:
            item['text_may_be_truncated'] = (not item['text'] or
                item['text'].rstrip().endswith(('…', '...')) or (row.get('message') or {}).get('is_truncated') is not False)
            if item['published_at']:
                item['facebook_date_source'] = 'facebook_cli_created_at'
            if story_id.isdigit():
                item['facebook_story_id'] = story_id
            result[item['canonical_key']] = item
        if len(result) >= limit:
            break
    return list(result.values())


def supports(source):
    if source['platform'] == 'facebook':
        return facebook_command(source, 1) is not None
    return source['platform'] == 'x' and x_target(source['url']) is not None


async def read(source, limit):
    require_cloud()
    platform = source['platform']
    tool = {'facebook': 'facebook-cli', 'x': 'x2md'}[platform]
    commit = PINS[tool]['commit']
    attempt = {'tool': tool, 'upstream_commit': commit, 'posts': 0}
    if not source_valid(source) or not 1 <= limit <= 100:
        return [], {**attempt, 'status': 'invalid_source'}
    root = tool_root()
    try:
        with tempfile.TemporaryDirectory(prefix='argus-facebook-x-') as temp:
            work = Path(temp)
            output = work / 'result.json'
            if platform == 'facebook':
                args = facebook_command(source, limit)
                if not args:
                    return [], {**attempt, 'status': 'unsupported_source_shape'}
                cmd = [str(root / 'fb'), *args, '-o', 'jsonl', '--tier', '0', '--retries', '0', '--timeout', '10s']
            else:
                if not x_target(source['url']):
                    return [], {**attempt, 'status': 'unsupported_source_shape'}
                cmd = [sys.executable, str(Path(__file__).with_name('x_post_details.py')),
                       str(root), json.dumps(source), str(output)]
            code, status, stdout = await process(cmd, work, output, timeout=45)
            if not status and code:
                codes = {3: 'no_public_posts', 4: 'login_required', 5: 'http_429', 6: 'unavailable', 8: 'network_error'}
                status = codes.get(code, 'worker_failed') if platform == 'facebook' else 'worker_failed'
            if status:
                return [], {**attempt, 'status': status}
            if platform == 'facebook':
                rows = [json.loads(line) for line in stdout.read_text(encoding='utf-8').splitlines() if line]
                items = facebook_rows(source, rows, limit)
            else:
                result = json.loads(output.read_text(encoding='utf-8'))
                items, status = result['items'], result['status']
            for item in items:
                item.update(capture_mode=tool, collector_commit=commit)
            return items, {**attempt, 'posts': len(items), 'status': 'ok' if items else status or 'no_public_posts'}
    except FileNotFoundError:
        return [], {**attempt, 'status': 'tool_unavailable'}
    except Exception as error:
        return [], {**attempt, 'status': error_code(error)}


async def enrich_one(source, items, attempts):
    """Read at most one verified permalink. Retain the rest of the existing feed."""
    from cloud_social import STOP
    if any(a.get('tool') in ('facebook-cli', 'x2md') or a.get('status') in STOP | {'unexpected_destination'} for a in attempts):
        return
    for item in items:
        url, key = item['post_url'], item['canonical_key']
        if item.get('capture_mode') in ('facebook-cli', 'x2md'):
            continue
        if source['platform'] == 'facebook':
            target = identity(url)
            if not target or target['canonical_key'] != key:
                continue
            if item.get('text') and item.get('published_at') and item.get('text_may_be_truncated') is False:
                continue
        else:
            target = x_target(url)
            if not target or key != 'x:' + target[2]:
                continue
        rows, attempt = await read({**source, 'url': url}, 1)
        attempts.append(attempt)
        if rows:
            row = rows[0]
            same = row['canonical_key'] == key or (source['platform'] == 'facebook'
                   and key == 'facebook:' + str(row.get('facebook_story_id') or ''))
            if same:
                if len(row['text']) > len(item['text']) and (item.get('text_may_be_truncated') is not False or row.get('text_may_be_truncated') is False):
                    item['text'] = row['text']
                    item['text_may_be_truncated'] = row.get('text_may_be_truncated', True)
                item['media_urls'] = list(dict.fromkeys(item.get('media_urls', []) + row['media_urls']))[:12]
                item['published_at'] = item.get('published_at') or row['published_at']
                if row.get('published_at') == item['published_at'] and row.get('facebook_date_source'):
                    item['facebook_date_source'] = row['facebook_date_source']
                item['detail_reader'] = attempt['tool']
        break
