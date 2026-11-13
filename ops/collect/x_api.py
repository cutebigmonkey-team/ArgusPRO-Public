"""Authenticated X v2 timelines and recent searches. Cursors are committed by the archive writer."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from candidate_audit import count
from run import error_code, now, post, source_valid
from source_health import instant, note_retry_after

HANDLE = re.compile(r'[A-Za-z0-9_]{1,15}')
ID = re.compile(r'[0-9]{1,19}')
RESERVED = {'search', 'home', 'explore', 'intent', 'i', 'settings', 'notifications', 'messages'}


def target(source):
    if source.get('platform') != 'x' or not source_valid(source):
        return None
    url = urlsplit(source['url'])
    path = url.path.strip('/')
    if path == 'search':
        queries = parse_qs(url.query).get('q', [])
        if len(queries) == 1 and queries[0].strip():
            return 'search', queries[0]
    if HANDLE.fullmatch(path) and path.lower() not in RESERVED:
        return 'timeline', path
    return None


def is_profile(source):
    resolved = target(source)
    return bool(resolved and resolved[0] == 'timeline')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(path, params, bearer):
    url = 'https://api.x.com/2/' + path + '?' + urlencode(params)
    req = Request(url, headers={'Authorization': 'Bearer ' + bearer, 'Accept': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(req, timeout=12) as response:
            raw = response.read(8 * 1024**2 + 1)
            if len(raw) > 8 * 1024**2:
                raise ValueError('response_too_large')
            return json.loads(raw)
    except HTTPError as exc:
        if exc.code == 429:
            try:
                reset = datetime.fromtimestamp(int(exc.headers['x-rate-limit-reset']), timezone.utc)
                note_retry_after(str(max(0, int(reset.timestamp()-time.time())+1)), datetime.now(timezone.utc))
            except (ValueError, TypeError, OverflowError, OSError):
                pass
        raise


def page_rows(source, data, owner_id, name):
    includes = data.get('includes', {})
    authors = {u['id']: u['username'] for u in includes.get('users', [])}
    media = {m['media_key']: m for m in includes.get('media', [])}
    rows = []
    for tweet in data.get('data', []):
        sid = tweet.get('id','') if isinstance(tweet,dict) else ''
        author = tweet.get('author_id') if isinstance(tweet,dict) else None
        if not isinstance(sid,str) or not ID.fullmatch(sid) or not author or (owner_id and author != owner_id):
            count('x_identity_mismatch')
            continue
        handle = name if owner_id else authors.get(author)
        url = f'https://x.com/{handle}/status/{sid}' if handle and HANDLE.fullmatch(handle) else f'https://x.com/i/web/status/{sid}'
        links = []
        for key in tweet.get('attachments', {}).get('media_keys', []):
            part = media.get(key, {})
            variants = sorted(part.get('variants', []), key=lambda v: v.get('bit_rate', 0), reverse=True)
            links.extend([next((v['url'] for v in variants if v.get('content_type') == 'video/mp4' and v.get('url')), None),
                          part.get('url'), part.get('preview_image_url')])
        body = (tweet.get('note_post') or tweet.get('note_tweet') or {}).get('text') or tweet.get('text', '')
        row = post(source, 'x:'+sid, url, body, tweet.get('created_at'), links)
        row['platform_post_id']=sid
        conversation=tweet.get('conversation_id')
        if isinstance(conversation,str) and ID.fullmatch(conversation):row['conversation_id']=conversation
        from engagement_metrics import counters
        row['engagement'] = counters(tweet.get('public_metrics') or {})
        if not row['published_at'] or not (body or row['media_urls']):
            count('x_incomplete_post')
            continue
        row.update(capture_mode='x_api_v2', text_may_be_truncated=False,
                   x_references=[{'type':r['type'], 'id':r['id']} for r in tweet.get('referenced_posts',tweet.get('referenced_tweets', []))
                                 if r.get('type') in ('replied_to','retweeted','quoted') and ID.fullmatch(r.get('id',''))])
        rows.append(row)
    return rows


def collect(source, cursor, bearer, *, max_pages, max_posts, seconds, since=None, read=None):
    read = read or request
    kind, value = target(source)
    saved = dict(cursor.get('x_api') or {})
    items = {}
    pages = 0
    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + seconds
    state = None
    unavailable = None
    status = 'ok'
    try:
        owner = None
        if kind == 'timeline':
            user = read('users/by/username/'+value, {}, bearer).get('data', {})
            owner = user.get('id', '')
            if not ID.fullmatch(owner) or user.get('username','').lower() != value.lower():
                raise ValueError('x_user_lookup_failed')
            if owner != saved.get('owner_id'):
                saved = {'owner_id':owner}
            path = f'users/{owner}/tweets'
        else:
            path = 'tweets/search/recent'
            # Recent search exposes only the last seven days. If a paused
            # scan ages out, record the missing interval and poll the available range.
            floor = started-timedelta(days=7)+timedelta(seconds=15)
            boundary = saved.get('start_time') or (cursor.get('updated_at') if saved else since.isoformat() if since else None)
            if boundary and instant(boundary)<floor:
                unavailable = {'from':boundary, 'to':floor.isoformat(), 'reason':'x_search_retention'}
                saved = {'start_time':floor.strftime('%Y-%m-%dT%H:%M:%SZ')}
        params = {'max_results':100, 'post.fields':'created_at,attachments,note_post,public_metrics,conversation_id',
                  'expansions':'author_id,attachments.media_keys,referenced_posts', 'media.fields':'url,preview_image_url,variants'}
        if kind == 'search':
            params.update(query=value, sort_order='recency')
        if saved.get('since_id'):
            params['since_id'] = saved['since_id']
        elif saved.get('start_time') or since:
            if not saved.get('start_time'):
                saved['start_time'] = since.strftime('%Y-%m-%dT%H:%M:%SZ')
            params['start_time'] = saved['start_time']
        token_key = 'next_token' if kind == 'search' else 'pagination_token'
        visited = set()
        while pages < max_pages and len(items) < max_posts and time.monotonic() < deadline:
            token = saved.get('next_token')
            if token:
                if token in visited:
                    raise ValueError('x_pagination_cycle')
                visited.add(token)
                params[token_key] = token
            if saved.get('until_id'):
                params['until_id'] = saved['until_id']
            try:
                data = read(path, params, bearer)
            except HTTPError as exc:
                # Tokens can expire between jobs. Continue below the last complete page.
                if exc.code != 400 or not token:
                    raise
                params.pop(token_key)
                saved.pop('next_token')
                if saved.get('oldest_id'):
                    saved['until_id'] = saved['oldest_id']
                continue
            tweets = data.get('data', [])
            meta = data.get('meta', {})
            if not isinstance(tweets,list):
                raise ValueError('x_incomplete_response')
            count('raw_input',len(tweets))
            rows = page_rows(source, data, owner, value)
            ids = [int(r['canonical_key'][2:]) for r in rows]
            for row in rows:
                if row['canonical_key'] in items:
                    count('duplicate_identity')
                items[row['canonical_key']] = row
            pages += 1
            # A deleted/protected quoted post can be absent from expansions
            # while its referencing post is present and complete.
            ref_ids={r['id'] for row in rows for r in row['x_references']}
            partial=any(not error.get('resource_id') or error['resource_id'] not in ref_ids
                        for error in data.get('errors',[]))
            if len(rows)!=len(tweets) or meta.get('result_count')!=len(tweets) or partial:
                # Deliver good records but retry the incomplete page with the old cursor.
                raise ValueError('x_incomplete_response')
            if ids:
                saved['pending_head'] = str(max(max(ids), int(saved.get('pending_head',0))))
                saved['oldest_id'] = str(min(ids))
            next_token = meta.get('next_token')
            if next_token and next_token in visited:
                raise ValueError('x_pagination_cycle')
            if next_token:
                saved['next_token'] = next_token
            else:
                head = saved.get('pending_head') or saved.get('since_id')
                saved = {'owner_id':owner} if owner else {}
                if head:
                    saved['since_id'] = head
                else:
                    # Overlap indexing around a quiet poll, including the very first empty poll.
                    saved['start_time'] = (started-timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
            state = {'url':source['url'], 'updated_at':now(), 'x_api':dict(saved)}
            if not next_token:
                break
    except Exception as exc:
        status = error_code(exc)
    ordered = sorted(items.values(),key=lambda r:int(r['canonical_key'][2:]),reverse=True)
    if status=='ok' and not pages:
        status='timeout'
    if status == 'ok' and not ordered and not saved.get('next_token'):
        status = 'no_new_posts'
    attempt = {'tool':'x_api_v2','pages':pages,'posts':len(ordered),'status':status}
    if unavailable:
        attempt['unavailable_range'] = unavailable
    return ordered, status, [attempt], state
