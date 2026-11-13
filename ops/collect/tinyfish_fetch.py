"""Last-resort public post collection through the free TinyFish Fetch API."""
import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from browser_public import facebook_owned, page_error
from run import HOSTS, date, error_code, post, social_url, source_valid, telegram_html, url_ok
from social_links import identity


ENDPOINT = 'https://api.fetch.tinyfish.ai'
RETRYABLE = {'no_public_posts', 'no_verified_page', 'no_verified_posts', 'unverified_markup', 'timeout',
             'network_error', 'connection_reset', 'http_500', 'http_502', 'http_503', 'http_504'}
STOP = {'http_401', 'http_403', 'http_429', 'login_required', 'challenge_required',
        'rate_limited', 'unexpected_destination', 'private_destination'}
PATHS = {
    'threads': r'/(@[\w.]+)/post/([\w-]+)',
    'x': r'/([\w]+)/status/(\d+)',
    'instagram': r'/([\w.]+)/(?:p|reels?)/([\w-]+)',
    'tiktok': r'/(@[\w.-]+)/video/(\d+)',
    'ok': r'/((?:group/|profile/)?[\w.-]+)/(?:topic|statuses)/(\d+)',
}


def owned_key(source, url):
    platform = source['platform']
    if not url_ok(url) or urlsplit(url).hostname not in HOSTS[platform]:
        return None
    owner = urlsplit(source['url']).path.strip('/')
    path = urlsplit(url).path.rstrip('/')
    if platform in ('instagram', 'facebook'):
        candidate, requested = identity(url), identity(source['url'])
        if not candidate:
            return None
        if requested:
            return requested['canonical_key'] if requested == candidate else None
        if platform == 'facebook':
            return candidate['canonical_key'] if facebook_owned(source, {}, url) else None
    if platform == 'vk':
        match = re.fullmatch(r'/wall(-?(\d+)_\d+)', path)
        if match and (owner == path[1:] or
                      (match[1].startswith('-') and owner in ('club'+match[2], 'public'+match[2]))):
            return 'vk:' + match[1]
    elif platform in PATHS:
        match = re.fullmatch(PATHS[platform], path, re.ASCII)
        if match and (owner.lower() == match[1].lower() or owner == path[1:]):
            return platform + ':' + match[2]
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_pages(key, urls, **options):
    payload = {'urls': urls, 'format': 'html', 'ttl': 300, 'per_url_timeout_ms': 8000, **options}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                     headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
        data = response.read(4 * 1024**2 + 1)
    if len(data) > 4 * 1024**2:
        raise ValueError('response_too_large')
    return json.loads(data)


def checked_pages(payload, urls):
    pages = {}
    for row in payload['results']:
        url = row['url']
        if url not in urls or not url_ok(row.get('final_url')):
            continue
        final = urlsplit(row['final_url'])
        original = urlsplit(url)
        if (final.hostname != original.hostname or final.scheme != original.scheme
                or final.path.rstrip('/') != original.path.rstrip('/') or final.query != original.query):
            continue
        if not isinstance(row.get('text'), str) or page_error(row['final_url'], row['text']):
            continue
        pages[url] = row
    if not pages:
        errors = payload.get('errors') or []
        code = errors[0].get('error') if errors else None
        raise ValueError(code if code in {'timeout','rate_limited','login_required','selector_not_matched',
                                          'robots_denied','challenge_required'} else 'no_verified_page')
    return pages


class TinyFetch:
    def __init__(self):
        # Keep the key out of browser and third-party adapter subprocess environments.
        self.key = os.environ.pop('TINYFISH_API_KEY', '').strip()
        self.remaining = 12
        self.lock = asyncio.Lock()

    async def pages(self, urls, **options):
        self.remaining -= len(urls)
        payload = await asyncio.to_thread(fetch_pages, self.key, urls, **options)
        return checked_pages(payload, urls)

    async def collect(self, source, limit):
        platform, url = source['platform'], source['url']
        if platform == 'telegram':
            channel = re.fullmatch(r'/(?:s/)?([A-Za-z0-9_]{3,64})/?', urlsplit(url).path)
            if not channel:
                return []
            url = 'https://t.me/s/' + channel[1]
            rows = await self.pages([url], include_selectors=['.tgme_widget_message[data-post]'])
            return telegram_html(source, limit, rows[url]['text'])

        candidates = {}
        if key := owned_key(source, url):
            candidates[key] = url
        else:
            profile = (await self.pages([url], links=True))[url]
            for link in profile.get('links') or []:
                if key := owned_key(source, link):
                    candidates.setdefault(key, social_url(link))
        candidates = dict(list(candidates.items())[:min(3, limit, self.remaining)])
        if not candidates:
            return []
        pages = await self.pages(list(candidates.values()))
        items = []
        for key, link in candidates.items():
            row = pages.get(link)
            if not row:
                continue
            # Only an excerpt tied to the individual permalink and visible in its body.
            excerpt = (row.get('description') or '').strip()
            visible = BeautifulSoup(row['text'], 'html.parser').get_text(' ', strip=True)
            normalized = ' '.join(excerpt.split()).rstrip('….')
            if len(normalized) < 12 or normalized not in ' '.join(visible.split()) or not date(row.get('published_date')):
                continue
            item = post(source, key, link, excerpt, row['published_date'])
            item['text_may_be_truncated'] = True
            items.append(item)
        return items

    async def recover(self, source, limit, items, status, attempts):
        if (items or not self.key or status not in RETRYABLE or not source_valid(source)
                or source['platform'] not in {*PATHS, 'facebook', 'vk', 'telegram'}
                or any(a['status'] in STOP or a['status'].startswith('robots_') for a in attempts)):
            return items, status, attempts
        # Catalog profile URLs only. Never send arbitrary query parameters to a provider.
        parsed = urlsplit(source['url'])
        if parsed.query and not (source['platform'] == 'facebook' and parsed.path == '/profile.php'
                                 and re.fullmatch(r'id=\d+', parsed.query)):
            return items, status, attempts
        async with self.lock:
            if self.remaining <= 0:
                return items, status, attempts
            try:
                items = await self.collect(source, limit)
                outcome = 'ok' if items else 'no_public_posts'
            except Exception as error:
                outcome = error_code(error)
                if outcome in {'http_401','http_402','http_403','http_429','rate_limited'}:
                    self.remaining = 0
        for item in items:
            item['capture_mode'] = 'tinyfish_fetch'
        return items, 'ok' if items else status, attempts + [
            {'tool': 'tinyfish_fetch', 'status': outcome, 'posts': len(items)}]
