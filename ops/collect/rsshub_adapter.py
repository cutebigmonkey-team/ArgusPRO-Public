"""RSSHub fallback for catalog YouTube channel URLs; verify channel ownership."""
import re
from urllib.parse import urlsplit,parse_qs
import feedparser


def channel(source):
    from run import source_valid
    if source.get('platform')!='youtube' or not source_valid(source):return None
    match=re.fullmatch(r'/channel/(UC[A-Za-z0-9_-]{22})/?',urlsplit(source['url']).path)
    return match[1] if match else None


def parse(body,source,limit):
    from run import feed_posts
    identity=channel(source)
    if not identity:return []
    feed=feedparser.parse(body)
    owner=urlsplit(feed.feed.get('link',''))
    if (owner.scheme!='https' or owner.hostname not in ('www.youtube.com','youtube.com')
            or owner.path.rstrip('/')!='/channel/'+identity or owner.username or owner.password):return []
    items=[]
    for item in feed_posts(source,body,min(limit,5)):
        url=urlsplit(item['post_url']);video=parse_qs(url.query).get('v',[''])[0]
        if url.hostname not in ('youtube.com','www.youtube.com') or url.path!='/watch' or not re.fullmatch(r'[A-Za-z0-9_-]{11}',video):continue
        item.update(canonical_key='youtube:'+video,capture_mode='rsshub',text_may_be_truncated=True)
        items.append(item)
    return items


def collect(source,limit,base):
    from run import fetch
    identity=channel(source)
    if not identity:return []
    body,_=fetch(base.rstrip('/')+'/youtube/channel/'+identity,timeout=15)
    return parse(body,source,limit)
