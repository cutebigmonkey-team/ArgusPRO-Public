"""Read a single verified X status using the pinned x2md renderer."""
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from cloud_social import require_cloud
from public_embeds import timestamp
from remaining_http import PublicHTTP, StopCollection
from run import HOSTS, error_code, post, url_ok


def x_target(url):
    if not url_ok(url) or urlsplit(url).netloc not in HOSTS['x']:
        return None
    return re.fullmatch(r'/([A-Za-z0-9_]{1,15})/status/(\d{1,22})/?', urlsplit(url).path)


def x_post(root, source):
    sys.path.insert(0, str(root / 'x2md'))
    import x2md
    target = x_target(source['url'])
    if not target:
        return []
    owner, sid = target.groups()
    http = PublicHTTP('x', budget=1)
    http.hosts = {'api.fxtwitter.com'}
    data = json.loads(http.request(f'https://api.fxtwitter.com/{owner}/status/{sid}'))
    if data.get('code') != 200:
        raise ValueError('upstream_rejected')
    tweet = data['tweet']
    link = x_target(tweet.get('url', ''))
    if not link or link[2] != sid or link[1].lower() != owner.lower() or str(tweet.get('id')) != sid:
        return []
    if tweet['author']['screen_name'].lower() != owner.lower():
        return []
    article = tweet.get('article')
    body = (article.get('title', '') + '\n\n' + x2md.render_article(article)).strip() if article else (
        tweet.get('text') or (tweet.get('raw_text') or {}).get('text') or '')
    media = tweet.get('media') or {}
    urls = [part.get('url') for key in ('photos', 'videos') for part in media.get(key, [])]
    if not body and not any(url_ok(url) for url in urls):
        return []
    return [post(source, 'x:' + sid, f'https://x.com/{owner}/status/{sid}', body,
                 timestamp(tweet.get('created_timestamp') or tweet.get('created_at')), urls)]


if __name__ == '__main__':
    require_cloud()
    root, source, output = sys.argv[1:]
    try:
        items = x_post(Path(root), json.loads(source))
        status = 'ok' if items else 'no_public_posts'
    except StopCollection as error:
        items, status = [], error.status
    except Exception as error:
        items, status = [], error_code(error)
    Path(output).write_text(json.dumps({'items': items, 'status': status}), encoding='utf-8')
