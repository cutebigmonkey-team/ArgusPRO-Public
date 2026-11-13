"""Credential-isolated cloud worker. Never serialize upstream exceptions."""
import dataclasses
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit, urlencode

from bs4 import BeautifulSoup
from remaining_http import PublicHTTP, StopCollection
from remaining_social import owner, same_owner


def profile(source):
    path = urlsplit(source['url']).path
    if source['platform'] == 'instagram' and not re.fullmatch(r'/[A-Za-z0-9_.]{1,30}/?', path):
        raise StopCollection('unsupported_source_shape')
    return owner(source['url'])


def document(source, http):
    platform, name = source['platform'], profile(source)
    if platform == 'instagram':
        return http.request('https://i.instagram.com/api/v1/users/web_profile_info/?' + urlencode({'username':name}),
                            headers={'x-ig-app-id':'936619743392459'})
    host = {'facebook':'m.facebook.com','vk':'vk.com','ok':'ok.ru'}[platform]
    u = urlsplit(source['url'])
    return http.request('https://' + host + u.path + ('?' + u.query if u.query else ''))


def evidence_rows(source, rows, body):
    """Bind Skraper IDs to actual permalinks and authors in its input response."""
    platform, expected = source['platform'], owner(source['url'])
    result = []
    if platform == 'instagram':
        user = (json.loads(body).get('data') or {}).get('user') or {}
        if not same_owner(expected, user.get('username', '')) or user.get('is_private'):
            return []
        nodes = {str(e['node'].get('id')):e['node'] for e in
                 (user.get('edge_owner_to_timeline_media') or {}).get('edges', []) if e.get('node')}
        for row in rows:
            node = nodes.get(str(row.get('id')), {})
            code = node.get('shortcode', '')
            author = node.get('owner') or {}
            if author.get('id') and str(author['id']) != str(user.get('id')):
                continue
            if not re.fullmatch(r'[A-Za-z0-9_-]+', code):
                continue
            result.append({**row, 'url':'https://www.instagram.com/p/'+code+'/', 'source_owner':expected})
        return result
    soup = BeautifulSoup(body, 'html.parser')
    selector = {'facebook':'article', 'vk':'.post', 'ok':'[data-feed-id]'}[platform]
    for card in soup.select(selector):
        links = [urljoin(source['url'], a['href']) for a in card.select('a[href]')]
        if platform == 'ok' and card.get('data-seen-params'):
            try:
                envelope = json.loads(card['data-seen-params'])
                context = envelope.get('data') or {}
            except (ValueError, TypeError):
                continue
            # Current OK group pages use vanity handles in author/permalink
            # URLs even when the catalog uses a numeric /group/ URL. The card's
            # own structured context binds group, feed ID and topic ID together.
            rid = card.get('data-feed-id')
            topic = str(context.get('mediaTopicId',''))
            if (envelope.get('type') != 'FEED' or context.get('feedLocation') != 'GROUP'
                    or expected != 'group/' + str(context.get('ownerContentId',''))
                    or context.get('feedId') != rid or context.get('isFriendsOnly') is not False
                    or not re.fullmatch(r'\d+', topic)):
                continue
            anchors = [a for a in card.select('a[href]')
                       if re.search(r'/(?:topic|statuses)/'+re.escape(topic)+r'(?:[/?#]|$)', a['href'])]
            if not anchors:
                continue
            for row in rows:
                if str(row.get('id','')) != rid:
                    continue
                media = list(row.get('media') or [])
                # New OK photo markup differs from the pinned parser's legacy
                # collage selector. Retain photos linked to this exact topic.
                media.extend(urljoin(source['url'], img['src']) for a in anchors for img in a.select('img[src]'))
                result.append({**row, 'url':urljoin(source['url'],anchors[0]['href']),
                               'source_owner':expected, 'media':list(dict.fromkeys(media))[:12]})
            continue
        # Only explicit author/profile links qualify; links nested in text or
        # reposts cannot establish the owner of the surrounding card.
        authors = card.select('header a[href], .PostHeaderTitle a[href], .post_author a[href], .feed_author a[href], .feed-author a[href], .feed_ava a[href]')
        authors = [owner(urljoin(source['url'], a['href'])) for a in authors]
        if expected not in authors:
            continue
        for row in rows:
            rid = str(row.get('id', ''))
            if not rid:
                continue
            if platform == 'ok':
                if card.get('data-feed-id') != rid:
                    continue
                urls = [u for u in links if re.search(r'/(?:topic|statuses)/\d+', u)]
            elif platform == 'vk':
                urls = [u for u in links if urlsplit(u).path.strip('/') == rid and re.fullmatch(r'wall-?\d+_\d+', rid)]
            else:
                from run import IDENTITIES
                urls = [u for u in links if (m := re.search(IDENTITIES['facebook'], u)) and m[1] == rid]
            if urls:
                result.append({**row, 'url':urls[0], 'source_owner':expected})
    return result


def skraper(source, tools, http, work, diagnostics=None, limit=5):
    body = document(source, http)
    raw, output = work/'page', work/'skraper.json'
    raw.write_text(body, encoding='utf-8')
    subprocess.run(['java', '-Xmx256m', '-cp', str(tools/'skraper/cli/target/cli.jar'),
                    'ru.sokomishalov.skraper.cli.ArgusOffline', source['platform'],
                    urlsplit(source['url']).path, str(raw), str(output), str(limit)],
                   timeout=18, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parsed = json.loads(output.read_text())
    verified = evidence_rows(source, parsed, body)
    if diagnostics is not None:
        from run import IDENTITIES
        soup = BeautifulSoup(body, 'html.parser')
        diagnostics.update(response_bytes=len(body.encode('utf-8')), raw_posts=len(parsed), verified_posts=len(verified),
                           post_links=sum(bool(re.search(IDENTITIES[source['platform']],a['href'])) for a in soup.select('a[href]')))
    if parsed and not verified:
        raise StopCollection('unverified_posts')
    return verified


def fbgql(source, http, limit=5, diagnostics=None):
    from fbgql import runner, ScrapeJob, config
    from fbgql.errors import DocIdStale, RateLimited, RequestRejected, SessionInvalid
    from run import IDENTITIES
    # Upstream posts_only does not hydrate a single-post stub. Do not turn the
    # stub into an empty or manufactured record; other adapters handle URLs.
    if re.search(IDENTITIES['facebook'], source['url']):
        raise StopCollection('unsupported_source_shape')
    if diagnostics is None:
        diagnostics = {}
    diagnostics['graphql_calls'] = 0
    last_count = 0
    continuations = 0
    def more(posts, job):
        nonlocal last_count, continuations
        if len(posts) <= last_count or len(posts) >= limit or getattr(http,'budget',1) < 1:
            return False
        last_count = len(posts)
        continuations += 1
        return True
    class Transport:
        def get(self, url, cookies=None, proxy=None, headers=None):
            return http.request(url, headers=headers)
        def post_form(self, headers, data, cookies=None, proxy=None):
            diagnostics['graphql_calls'] += 1
            try:
                return http.request(config.GRAPHQL_URL, 'POST', headers, data)
            except StopCollection as error:
                if not last_count:
                    raise
                diagnostics['pagination_stop'] = error.status
                raise RequestRejected(error.status) from None
    original_transport = runner.SyncTransport
    runner.SyncTransport = Transport
    # Keep the first page and stop when a continuation yields no new IDs.
    original_cap = runner._under_post_cap
    runner._under_post_cap = more
    job = ScrapeJob(page=source['url'], max_posts=limit, posts_only=True, anonymous=True,
                    workers=1, max_comments=0, reply_fb_cap=0, min_interval_sec=1)
    try:
        plan = runner.prepare(job)
    except DocIdStale:
        raise StopCollection('graphql_query_stale')
    except RateLimited:
        raise StopCollection('http_429')
    except SessionInvalid:
        raise StopCollection('login_required')
    except RequestRejected:
        raise StopCollection('graphql_request_rejected')
    except ValueError as error:
        if str(error).startswith("Could not resolve numeric id"):
            raise StopCollection('page_id_unresolved')
        raise
    finally:
        runner._under_post_cap = original_cap
        runner.SyncTransport = original_transport
    diagnostics['raw_posts'] = len(plan.posts)
    expected = owner(source['url'])
    rows = []
    for post in plan.posts[:limit]:
        p = dataclasses.asdict(post)
        url = p.get('permalink') or ''
        url = facebook_permalink(url, p.get('post_id'), plan.resolved_page_id)
        actual = owner(url)
        # A numeric permalink is allowed only for the page resolved by this
        # very execution plan; unrelated feed recommendations are discarded.
        if actual != expected and actual != str(plan.resolved_page_id):
            continue
        rows.append({'url':url, 'source_owner':expected, 'text':p.get('text'),
                       'published':p.get('created_time'), 'facebook_story_id':p.get('post_id'), 'media':[]})
    return rows


def facebook_permalink(url, post_id, owner_id):
    """Expand numeric FB links only when the parser supplied matching post IDs."""
    u = urlsplit(url)
    if (u.hostname in ('www.facebook.com','facebook.com','m.facebook.com','mbasic.facebook.com')
            and re.fullmatch(r'\d+', str(post_id or '')) and re.fullmatch(r'\d+', str(owner_id or ''))
            and u.path.strip('/') == str(post_id)):
        return 'https://www.facebook.com/permalink.php?' + urlencode({'story_fbid':post_id,'id':owner_id})
    return url


def socialfetch(source, tools, http, limit=5):
    loader = importlib.machinery.SourceFileLoader('argus_socialfetch', str(tools/'socialfetch/socialfetch'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module._request = lambda url, timeout=12, ua=None, extra_headers=None: (200, http.request(url, headers=extra_headers))
    args = SimpleNamespace(timeout=12, limit=limit, target=source['url'], ua=module.DEFAULT_UA)
    if source['platform'] == 'instagram':
        name = profile(source)
        raw = module._ig_feed(name, args)
        rows = []
        for item in raw.get('items', [])[:limit]:
            if not same_owner(name, (item.get('user') or {}).get('username', '')):
                continue
            parsed = module._ig_item(item)
            rows.append({'url':parsed['url'], 'source_owner':name, 'text':parsed['caption'],
                         'published':parsed['taken_at'], 'media':parsed['media']})
        return rows[:limit]
    from run import IDENTITIES
    if not re.search(IDENTITIES['facebook'], source['url']):
        raise StopCollection('unsupported_source_shape')
    parsed = module.fb_post(args)
    soup = BeautifulSoup(parsed.get('html') or '', 'html.parser')
    quote = soup.select_one('blockquote')
    if not quote:
        return []
    return [{'url':source['url'], 'text':quote.get_text(' ', strip=True), 'media':[]}]


def granary_rows(source, activities):
    from run import plain
    rows = []
    for activity in activities:
        obj = activity.get('object') or activity
        author = obj.get('author') or activity.get('actor') or {}
        author_url = author.get('url') or ''
        images = obj.get('image') or []
        if isinstance(images, dict):
            images = [images]
        media = [i['url'] for i in images if isinstance(i, dict) and i.get('url')]
        for attachment in obj.get('attachments') or []:
            for key in ('image','stream'):
                value = attachment.get(key) or {}
                if isinstance(value,dict) and value.get('url'):
                    media.append(value['url'])
        url = obj.get('url') or activity.get('url') or ''
        if source['platform'] == 'facebook':
            url = facebook_permalink(url, obj.get('fb_id'), str(author.get('id','')).rsplit(':',1)[-1])
        rows.append({'url':url,
                     'source_owner':owner(author_url), 'text':plain(obj.get('content') or ''),
                     'published':obj.get('published'), 'media':media})
    return rows


def granary(source, http, limit=5):
    if source['platform'] == 'instagram':
        body = document(source, http)
        from granary.instagram import Instagram
        activities, _ = Instagram().scraped_json_to_as1(json.loads(body), count=limit, fetch_extras=False)
    else:
        from granary.facebook import Facebook
        from run import IDENTITIES
        u = urlsplit(source['url'])
        body = http.request('https://mbasic.facebook.com' + u.path + ('?' + u.query if u.query else ''))
        if re.search(IDENTITIES['facebook'], source['url']):
            activity, _ = Facebook().scraped_to_as1_activity(body)
            activities = [activity] if activity else []
        else:
            activities, _ = Facebook().scraped_to_as1_activities(body)
    return granary_rows(source, activities[:limit])


def vkrss(source, tools, work, limit=5):
    import datetime as dt
    import calendar
    import feedparser
    from run import plain
    target = owner(source['url'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', target):
        raise StopCollection('unsupported_source_shape')
    output = work/'vk.json'
    subprocess.run(['php', str(Path(__file__).with_name('vkrss_runner.php')), str(tools/'vkrss'),
                    target, str(output),str(limit)], timeout=35, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = json.loads(output.read_text())
    if data['status'] != 'ok':
        raise StopCollection(data['status'])
    feed = feedparser.parse(data['rss'])
    return [{'url':e.get('link', ''), 'source_owner':target,
             'text':plain(e.get('summary', '')), 'media':[],
             'published':dt.datetime.fromtimestamp(calendar.timegm(e['published_parsed']),dt.timezone.utc).isoformat()
             if e.get('published_parsed') else None} for e in feed.entries[:limit]]


def main():
    from cloud_social import require_cloud
    require_cloud()
    request, output = map(Path, sys.argv[1:])
    job = json.loads(request.read_text())
    source, tool, tools = job['source'], job['tool'], Path(job['tools'])
    # A Facebook page can expose multiple candidate IDs before the first timeline response.
    http = PublicHTTP(source['platform'], budget=12 if tool == 'fbgql' else 5)
    limit=job.get('limit',5)
    if type(limit) is not int or not 1<=limit<=100: raise ValueError('invalid_limit')
    diagnostics = {}
    try:
        if tool == 'skraper': rows = skraper(source, tools, http, request.parent, diagnostics,limit)
        elif tool == 'fbgql': rows = fbgql(source, http,limit,diagnostics)
        elif tool in ('TikTok-Api', 'snscrape'):
            from short_posts import read
            rows = read(tool, source['url'], limit)
        elif tool == 'socialfetch': rows = socialfetch(source, tools, http,limit)
        elif tool == 'granary': rows = granary(source, http,limit)
        elif tool == 'vkrss': rows = vkrss(source, tools, request.parent,limit)
        else: raise StopCollection('unsupported_tool')
        result = {'rows':rows, 'status':diagnostics.get('pagination_stop') or ('ok' if rows else 'no_public_posts')}
    except StopCollection as error:
        result = {'rows':[], 'status':error.status}
    except subprocess.TimeoutExpired:
        result = {'rows':[], 'status':'timeout'}
    except Exception as error:
        # Only class name for diagnosis, never upstream messages/HTML/tokens.
        result = {'rows':[], 'status':'extraction_failed', 'error_type':type(error).__name__}
    if diagnostics:
        result['details'] = diagnostics
    output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
