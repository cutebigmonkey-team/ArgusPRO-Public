"""Bounded Instagram RSS-Bridge client. The public operator controls its cache."""
import re
import threading
import time
import urllib.error
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup
import feedparser
from candidate_audit import count

GATEWAY='https://rss-bridge.sans-nuage.fr/'
_lock=threading.Lock()
_last_finished=0.0
_limited=False


def profile(source):
    from run import source_valid
    if source.get('platform')!='instagram' or not source_valid(source):raise ValueError('invalid_source')
    name=urlsplit(source['url']).path.strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_.]{1,30}',name):raise ValueError('invalid_profile')
    return name


def parse(body,source,limit):
    from run import HOSTS, plain, post, feed_date
    name=profile(source);feed=feedparser.parse(body);items={}
    count('raw_input',len(feed.entries))
    # Post URLs contain no owner. Verify the feed's original profile URL first.
    try:
        owner=urlsplit(feed.feed.get('link',''))
        if (owner.scheme!='https' or owner.hostname not in HOSTS['instagram'] or
            owner.username or owner.password or owner.port not in (None,443) or
            owner.path.strip('/').lower()!=name.lower()):return []
    except ValueError:return []
    for entry in feed.entries:
        try:
            url=urlsplit(entry.get('link',''))
            identity=re.fullmatch(r'/(?:p|reel)/([A-Za-z0-9_-]+)/?',url.path)
            if (url.scheme!='https' or url.hostname not in HOSTS['instagram'] or not identity or
                url.username or url.password or url.port not in (None,443)):
                count('invalid_instagram_permalink')
                continue
            # Reject recommendations/reposts attributed to a different owner.
            authors=entry.get('authors',[])
            if authors and any(str(a.get('name','')).lstrip('@').lower()!=name.lower() for a in authors):
                count('source_owner_mismatch')
                continue
            published=feed_date(entry)
            content=next(iter(entry.get('content',[])),{}).get('value','') or entry.get('summary','')
            text=plain(content) or plain(entry.get('title',''))
            media=[e.get('href','') for e in entry.get('enclosures',[])]+[e.get('url','') for e in entry.get('media_content',[])]
            media += [e.get('src','') for e in BeautifulSoup(content,'html.parser').select('img[src],video[src],source[src]')]
            if not text and not media:
                count('no_public_content')
                continue
            item=post(source,'instagram:'+identity[1],'https://www.instagram.com/p/'+identity[1]+'/',text,published,media)
            item.update(capture_mode='public_rss_bridge',gateway_url=GATEWAY,text_may_be_truncated=True)
            items[item['canonical_key']]=item
            if len(items)>=limit:break
        except (ValueError,TypeError,OverflowError,IndexError):
            count('invalid_instagram_entry')
            continue
    return list(items.values())


def collect(source,limit,*,metrics=None):
    from run import fetch
    global _last_finished,_limited
    name=profile(source)
    url=GATEWAY+'?'+urlencode({'action':'display','bridge':'Instagram','context':'Username','u':name,'format':'Atom'})
    # Never queue network work behind a timed-out asyncio.to_thread caller.
    # Keep the conservative operator protection; no provider concurrency grant
    # has been established. The scheduler remains responsible for fair retries.
    metrics=metrics if metrics is not None else {}
    metrics.update(gateway_admitted=False,gateway_request_seconds=0.0)
    if not _lock.acquire(blocking=False):raise ValueError('gateway_busy')
    try:
        if _limited:raise ValueError('gateway_rate_limited')
        remaining=max(0,5-(time.monotonic()-_last_finished))
        if remaining:
            metrics['gateway_retry_seconds']=round(remaining,3)
            raise ValueError('gateway_cooldown')
        metrics['gateway_admitted']=True
        started=time.monotonic()
        try:
            body,_=fetch(url,timeout=25)
            return parse(body,source,limit)
        except urllib.error.HTTPError as error:
            if error.code==429:_limited=True
            raise
        finally:
            _last_finished=time.monotonic()
            metrics['gateway_request_seconds']=round(_last_finished-started,3)
    finally:_lock.release()
