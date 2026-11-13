"""Bounded public collection on GitHub-hosted runners. No database or Drive credentials.

Only post-shaped records are accepted. Login walls, challenges and absent sources
produce reports, never synthetic posts. Media URLs are retained, not downloaded.
"""
import argparse
import asyncio
from email.utils import parsedate_to_datetime
import collections
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

from bs4 import BeautifulSoup
import feedparser
from record_quality import merge_observations
from source_health import instant, reader_history, platform_status, effective_status
import candidate_audit
from url_identity import url_ok

PLATFORMS = ('telegram','youtube','x','facebook','instagram','rss','tiktok','threads','vk','ok','max','reddit','rutube','mailru','whatsapp','gdelt')
HOSTS = {
    'telegram': ('t.me',), 'youtube': ('youtube.com','www.youtube.com','m.youtube.com'),
    'x': ('x.com','www.x.com','twitter.com','www.twitter.com'),
    'facebook': ('facebook.com','www.facebook.com','m.facebook.com','mbasic.facebook.com'),
    'instagram': ('instagram.com','www.instagram.com'),
    'tiktok': ('tiktok.com','www.tiktok.com','vt.tiktok.com','vm.tiktok.com'), 'threads': ('threads.net','www.threads.net','threads.com','www.threads.com'),
    'vk': ('vk.com','www.vk.com','m.vk.com','vk.ru','www.vk.ru'),
    'ok': ('ok.ru','www.ok.ru','m.ok.ru'), 'max': ('max.ru','web.max.ru','www.max.ru'),
    'reddit': ('reddit.com','www.reddit.com','old.reddit.com'),
    'rutube': ('rutube.ru','www.rutube.ru'), 'whatsapp': ('whatsapp.com','www.whatsapp.com'),
    'gdelt': ('api.gdeltproject.org',),
}
IDENTITIES = {
    'facebook': r'(?:/(?:posts|permalink|videos|reel|reels)/|[?&](?:story_fbid|fbid)=)([A-Za-z0-9_-]+)',
    'instagram': r'/(?:p|reel)/([A-Za-z0-9_-]+)', 'x': r'/status/(\d+)',
    'threads': r'/post/([A-Za-z0-9_-]+)', 'tiktok': r'/video/(\d+)',
    'vk': r'/wall(-?\d+_\d+)', 'ok': r'/(?:topic|statuses)/(\d+)',
    'max': r'/c/(-?\d+/[A-Za-z0-9_-]+)',
}

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()

@lru_cache(maxsize=4096)
def public_host(host):
    addresses = socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('private_destination')

def safe_url(url):
    if not url_ok(url): raise ValueError('invalid_url')
    public_host(urllib.parse.urlsplit(url).hostname)

class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(url, timeout=12):
    from http_pool import request
    for _ in range(6):
        safe_url(url)
        try:
            return request(url,{'User-Agent':'ArgusPRO/1.0 public-feed-reader',
                           'Accept':'application/atom+xml,application/rss+xml,text/html;q=0.8,*/*;q=0.5'},timeout)
        except urllib.error.HTTPError as error:
            if error.code not in (301,302,303,307,308) or not error.headers.get('Location'):raise
            url=urllib.parse.urljoin(url,error.headers['Location'])
    raise ValueError('redirect_limit')

def plain(value): return BeautifulSoup(str(value),'html.parser').get_text(' ',strip=True)[:100000]

def date(value):
    if not value: return None
    try:
        parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None: return None
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except (ValueError,TypeError): return None

def post(source, key, url, text, published=None, media=()):
    if not url_ok(url): raise ValueError('invalid_post_url')
    observed = now()
    return {'canonical_key':key,'platform':source['platform'],'post_url':url,'text':text[:100000],
            'published_at':date(published),'observed_at':observed,'collected_at':observed,
            'discovered_at':observed,'media':[],
            'media_urls':list(dict.fromkeys(u for u in media if isinstance(u,str) and url_ok(u)))[:12],
            'origin':source.get('origin') if source.get('origin') in ('general','dsns') else 'unknown',
            'source_id':str(source.get('id','')),'source_name':source.get('name',''),'source_url':source['url']}

def social_url(url):
    parsed=urllib.parse.urlsplit(url)
    # Preserve identity parameters; tracking is never identity.
    query=urllib.parse.urlencode([(k,v) for k,v in urllib.parse.parse_qsl(parsed.query) if k in ('story_fbid','fbid','id')])
    return urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,parsed.path,query,''))

def telegram(source, limit):
    channel=urllib.parse.urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    if not re.fullmatch(r'[A-Za-z0-9_]{3,64}',channel): raise ValueError('invalid_channel')
    body,_=fetch('https://t.me/s/'+channel)
    return telegram_html(source,limit,body)


def telegram_html(source,limit,body):
    channel=urllib.parse.urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    soup=BeautifulSoup(body,'html.parser'); items=[]
    for node in soup.select('.tgme_widget_message[data-post]'):
        identity=node.get('data-post','')
        if not re.fullmatch(re.escape(channel)+r'/\d+',identity,re.I): continue
        text=node.select_one('.tgme_widget_message_text')
        stamp=node.select_one('.tgme_widget_message_date time[datetime]') or node.select_one('time[datetime]')
        parts=[text.get_text('\n',strip=True)] if text else []
        for detail in node.select('.tgme_widget_message_document_title,.tgme_widget_message_poll_question,.tgme_widget_message_poll_option_text'):
            value=detail.get_text(' ',strip=True)
            if value and value not in parts:parts.append(value)
        media=[]
        for element in node.select('[style]'):
            media.extend(re.findall(r"url\(['\"]?(https?://[^)'\"]+)",element.get('style','')))
        media += [element.get('src') for element in node.select('video[src],audio[src],video source[src],audio source[src],img.tgme_widget_message_sticker[src]')]
        media += [element['href'] for element in node.select('a.tgme_widget_message_document[href]') if url_ok(element['href']) and urllib.parse.urlsplit(element['href']).hostname!='t.me']
        if not any(parts) and not media: continue
        item=post(source,'telegram:'+identity.lower(),'https://t.me/'+identity,
                  '\n'.join(parts),stamp.get('datetime') if stamp else None,media)
        from engagement_metrics import telegram_counters
        metrics = telegram_counters(node)
        if metrics: item['engagement'] = metrics
        from social_links import outbound
        item.update(outbound(str(text) if text else ''))
        items.append(item)
    return items[-limit:]

def feed_posts(source, body, limit, *, base_url=None):
    from feed_formats import parse
    return feed_entries(source,parse(body).entries,limit,base_url=base_url)


def feed_entries(source, entries, limit, *, base_url=None):
    candidate_audit.count('raw_input',len(entries))
    items=[]
    seen = set()
    for entry in entries:
        url=entry.get('link','')
        if url and base_url and source['platform'] in ('rss','mailru'):
            url=urllib.parse.urljoin(base_url,url)
        if not url_ok(url):
            candidate_audit.reject('invalid_feed_permalink',url)
            continue
        platform=source['platform']
        if platform in ('rss','mailru'):
            from url_identity import clean_permalink
            url=clean_permalink(url)
        identity=entry.get('yt_videoid') if platform=='youtube' else None
        if platform=='reddit':
            path=urllib.parse.urlsplit(url)
            owner=re.match(r'/r/([\w]+)/?',urllib.parse.urlsplit(source['url']).path,re.I)
            target=re.fullmatch(r'/r/([\w]+)/comments/([a-z0-9]+)(?:/[^/]*)?/?',path.path,re.I)
            if not owner or not target or path.hostname not in HOSTS['reddit'] or owner[1].lower()!=target[1].lower():continue
            identity=target[2].lower()
            url='https://www.reddit.com/r/'+target[1]+'/comments/'+identity+'/'
        key=platform+':'+(identity or hashlib.sha256(url.split('#')[0].encode()).hexdigest())
        if key in seen:
            candidate_audit.reject('repeated_feed_identity',key)
            continue
        published=feed_date(entry)
        content=next(iter(entry.get('content',[])),{}).get('value','') or entry.get('summary','')
        text='\n\n'.join(dict.fromkeys(s for s in (plain(entry.get('title','')),plain(content)) if s))
        media=[e.get('href','') for e in entry.get('enclosures',[])]+[e.get('url','') for e in entry.get('media_content',[])]
        media.extend(e.get('url','') for e in entry.get('media_thumbnail',[]))
        item=post(source,key,url,text,published,media)
        if not text and not item['media_urls']:
            candidate_audit.reject('no_public_content',key)
            continue
        item['capture_mode']='native_feed'
        item['text_may_be_truncated']=not bool(entry.get('content') and plain(content)) or text.rstrip().endswith(('вЂ¦','...'))
        from social_links import outbound
        item.update(outbound(content,url))
        items.append(item)
        seen.add(key)
        if len(items)>=limit: break
    return items

def feed_date(entry):
    raw=entry.get('published')
    if not raw:return None
    iso=date(raw)
    if iso:return iso
    try:return date(parsedate_to_datetime(raw).isoformat())
    except (ValueError,TypeError,OverflowError):return None

def feeds(source, limit):
    url=source['url']; platform=source['platform']
    if platform=='youtube':
        from youtube_feed import collect as youtube_collect
        return youtube_collect(source,limit,fetch)
    elif platform=='reddit':
        match=re.search(r'/r/([A-Za-z0-9_]+)',url)
        if not match: raise ValueError('invalid_subreddit')
        url='https://www.reddit.com/r/'+match[1]+'/new/.rss'
        if os.getenv('ARGUS_ANONYMOUS_ONLY')=='1':
            from gdelt_public import permitted
            permitted(url,fetch)
    body,final=fetch(url); items=feed_posts(source,body,limit,base_url=final)
    if not items and platform in ('rss','mailru'):
        soup=BeautifulSoup(body,'html.parser')
        for link in soup.select('link[rel="alternate"][href]')[:4]:
            if link.get('type') not in ('application/rss+xml','application/atom+xml'): continue
            candidate=urllib.parse.urljoin(final,link['href'])
            if urllib.parse.urlsplit(candidate).hostname != urllib.parse.urlsplit(final).hostname: continue
            try:
                feed_body,feed_url=fetch(candidate);items=feed_posts(source,feed_body,limit,base_url=feed_url)
            except urllib.error.HTTPError as error:
                if error.code==429:raise
                continue
            except (ValueError,urllib.error.URLError,TimeoutError):continue
            if items: break
        if not items:
            from web_pages import article_rows,article_links
            records=article_rows(body,final)
            if not records:
                for link in article_links(body,final,min(limit,3)):
                    try:
                        article_body,article_url=fetch(link);records+=article_rows(article_body,article_url)
                    except (ValueError,urllib.error.URLError,TimeoutError):continue
            for row in records[:limit]:
                from article_enrichment import row_post
                items.append(row_post(source,row))
    return items

def rutube(source,limit):
    from web_pages import article_rows
    body,url=fetch(source['url']);items=[]
    rows=article_rows(body,url)
    candidate_audit.count('raw_input',len(rows))
    for row in rows[:limit]:
        item=post(source,'rutube:'+hashlib.sha256(row['url'].encode()).hexdigest(),row['url'],plain(row['text']),row['published_at'],row['media'])
        item['text_may_be_truncated']=True;items.append(item)
    return items

async def social(browser, source, limit):
    from browser_public import collect
    return await collect(browser,source,limit)



def source_valid(source):
    platform=source.get('platform'); url=source.get('url','')
    if platform not in PLATFORMS or not url_ok(url): return False
    if os.getenv('ARGUS_ANONYMOUS_ONLY')=='1' and any(key.lower() in {
        'token','access_token','api_key','apikey','session','sessionid','auth','cookie','csrf','password','oauth_token'}
        for key,_ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)):return False
    host=urllib.parse.urlsplit(url).hostname
    if platform=='instagram' and urllib.parse.urlsplit(url).path.strip('/').split('/')[0] in ('accounts','explore','direct'):return False
    return platform=='rss' or (platform=='mailru' and (host=='mail.ru' or host.endswith('.mail.ru'))) or host in HOSTS.get(platform,())

def error_code(error):
    if isinstance(error,urllib.error.HTTPError):
        if error.code in (429,503) and error.headers:
            from source_health import note_retry_after
            note_retry_after(error.headers.get('Retry-After'),dt.datetime.now(dt.timezone.utc))
        return 'http_'+str(error.code)
    if isinstance(error,(TimeoutError,asyncio.TimeoutError)): return 'timeout'
    if type(error).__module__.startswith('playwright') and type(error).__name__=='TimeoutError':return 'timeout'
    if isinstance(error,ValueError) and re.fullmatch(r'[a-z0-9_]{1,70}',str(error)): return str(error)
    if isinstance(error,urllib.error.URLError):
        reason=error.reason
        if isinstance(reason,ssl.SSLCertVerificationError):return 'tls_certificate_error'
        if isinstance(reason,ssl.SSLError):return 'tls_error'
        if isinstance(reason,socket.gaierror):return 'dns_error'
        if isinstance(reason,TimeoutError):return 'timeout'
        if isinstance(reason,ConnectionResetError):return 'connection_reset'
        if isinstance(reason,ConnectionRefusedError):return 'connection_refused'
        return 'network_error'
    return 'collection_failed'

async def collect_source(browser,source,limit,media_fallback=False,*,actor_token=None,primary_result=None):
    if (os.getenv('ARGUS_SOURCE_ADAPTERS') == '1' and source_valid(source)
            and source['platform'] in ('x', 'threads', 'instagram', 'tiktok')):
        from source_adapters import collect, supplement_threads
        items, status, attempts = await collect(browser, source, limit, media_fallback, collect_existing, actor_token, primary_result)
        if source['platform'] == 'threads' and items and status == 'ok' and os.getenv('ARGUS_SOCIAL_COLLECTION') == '1':
            await supplement_threads(source, limit, items, attempts)
        return items, status, attempts
    return await collect_existing(browser, source, limit, media_fallback)


async def collect_existing(browser,source,limit,media_fallback=False):
    import depth
    items=[];status='ok';attempts=[]
    trace=depth.attempt_trace.get()
    if trace is not None:trace['attempts']=attempts
    if not source_valid(source):return [],'invalid_source',[{'tool':'source_validation','status':'invalid_source','posts':0}]
    platform=source['platform']
    if platform in {'max','whatsapp'} and os.getenv('ARGUS_ANONYMOUS_ONLY')=='1':
        return [],'unsupported',[{'tool':'anonymous_policy','status':'unsupported','posts':0,
                                 'reason':'no_verified_permitted_anonymous_message_reader'}]
    if platform=='gdelt':
        from gdelt_public import collect as collect_gdelt
        try:
            items,status=await asyncio.to_thread(collect_gdelt,source,limit,fetch)
        except Exception as error:items,status=[],error_code(error)
        return items,status,[{'tool':'gdelt_public_json','status':status,'posts':len(items)}]
    remaining_enabled = os.environ.get('ARGUS_REMAINING_COLLECTION') == '1'
    if platform == 'threads' and os.getenv('ARGUS_SOCIAL_COLLECTION') != '1':
        from threads_public import collect as public_threads
        try:
            items, attempt = await asyncio.wait_for(asyncio.to_thread(public_threads, source, limit), timeout=12)
        except Exception as error:
            items, attempt = [], {'tool':'threads_public_json','status':error_code(error),'posts':0,'error_type':type(error).__name__}
        attempts.append(attempt)
        if items or attempt['status'] in {'http_401','http_403','http_429','login_required','challenge_required'} or attempt['status'].startswith('robots_'):
            return items, attempt['status'], attempts
    if platform == 'facebook':
        from source_health import preferred
        from facebook_feed import collect as collect_feed, BLOCKS
        from remaining_social import adapter
        graph_enabled = remaining_enabled or os.getenv('ARGUS_FACEBOOK_GRAPH') == '1'
        readers = (['fbgql'] if graph_enabled else []) + (['facebook_public_feed'] if browser else [])
        if os.getenv('ARGUS_REVIEWED_FACEBOOK_CLI') == '1':
            from facebook_x import read, supports
            if supports(source):
                readers.append('facebook-cli')
        verified = {}
        for tool in preferred(readers):
            started = time.monotonic()
            try:
                if tool == 'fbgql':
                    rows, attempt = await adapter(source, min(limit,100), tool)
                    status = attempt['status']
                    extra = [attempt]
                elif tool == 'facebook-cli':
                    rows, attempt = await read(source, min(limit,100))
                    status = attempt['status']
                    extra = [attempt]
                else:
                    rows, status, extra = await collect_feed(browser, source, limit)
            except Exception as error:
                rows, status = [], error_code(error)
                extra = [{'tool':tool, 'status':status, 'posts':0}]
            for attempt in extra:
                attempt['seconds'] = round(time.monotonic()-started,2)
                attempt['keys'] = sorted({row['canonical_key'] for row in rows})
            attempts.extend(extra)
            for row in rows:
                key = row['canonical_key']
                verified[key] = merge_observations(verified.get(key), row)
            if status in BLOCKS | {'unexpected_destination'}:
                return list(verified.values()) if depth.browser_progress.get() is not None else list(verified.values())[:limit], status, attempts
            if len(verified) >= limit and all(row.get('published_at') and row.get('text_may_be_truncated') is False for row in rows):
                break
        if verified or (readers and os.environ.get('ARGUS_FACEBOOK_BROWSER_ONLY')=='true'):
            return list(verified.values()) if depth.browser_progress.get() is not None else list(verified.values())[:limit], 'ok' if verified else status, attempts
    if platform=='max':
        from max_sdk import collect as max_collect
        return await max_collect(source,limit)
    if os.environ.get('ARGUS_FACEBOOK_X') == '1' and platform in ('facebook', 'x'):
        from cloud_social import STOP
        from facebook_x import read, supports
        if supports(source):
            items, attempt = await read(source, min(limit, 100))
            attempts.append(attempt)
            if items or attempt['status'] in STOP | {'unexpected_destination'}:
                return items, attempt['status'], attempts
    if platform == 'facebook' and os.environ.get('ARGUS_PUBLIC_SOCIAL_ADAPTERS') == '1':
        from public_cloud_adapters import hosted_sipher
        try:
            items, attempt = await hosted_sipher(source, min(limit, 100))
        except Exception as error:
            items, attempt = [], {'tool': 'sipher', 'status': error_code(error), 'posts': 0}
        attempts.append(attempt)
        if items:
            return items, 'ok', attempts
    if os.environ.get('ARGUS_SOCIAL_COLLECTION')=='1' and platform in ('telegram','threads'):
        from cloud_social import collect as cloud_collect
        items,attempts=await cloud_collect(source,min(limit,100))
        return items,attempts[-1]['status'],attempts
    from x_api import is_profile as x_profile
    profile_path=urllib.parse.urlsplit(source['url']).path
    if platform=='instagram' and re.fullmatch(r'/[A-Za-z0-9_.]{1,30}/?',profile_path):
        from rss_gateway import collect as gateway
        gateway_metrics={}
        gateway_started=time.monotonic()
        try:
            items=await asyncio.wait_for(asyncio.to_thread(gateway,source,limit,metrics=gateway_metrics),timeout=35)
            raw=len(items)
            items=candidate_audit.windowed(items)
            gateway_status='ok' if items else 'stale_feed_only' if raw else 'no_public_posts'
        except Exception as error:gateway_status=error_code(error)
        attempts.append({'tool':'rss_bridge','status':gateway_status,'posts':len(items),
                         'seconds':round(time.monotonic()-gateway_started,3),**dict(gateway_metrics)})
        if items:return items,'ok',attempts
    if os.environ.get('ARGUS_EMBED_COLLECTION')=='1' and platform in ('instagram','facebook'):
        from embed_social import collect as embed_collect, STOP
        items, extra_attempts = await embed_collect(source,min(limit,100))
        attempts.extend(extra_attempts)
        if items or attempts[-1]['status'] in STOP:return items,attempts[-1]['status'],attempts
    if os.environ.get('ARGUS_REMAINING_COLLECTION')=='1' and platform in ('instagram','facebook','vk','ok'):
        from remaining_social import collect as remaining_collect, STOP
        items, extra_attempts = await remaining_collect(source,min(limit,100),skip=('fbgql',))
        attempts.extend(extra_attempts)
        if items or attempts[-1]['status'] in STOP:
            status = attempts[-1]['status']
            return items,'ok' if items and status not in STOP else status,attempts
    if os.environ.get('ARGUS_SOCIAL_COLLECTION')=='1' and platform in ('instagram','facebook'):
        from cloud_social import library, STOP
        try:
            items,attempt=await library(source,min(limit,100))
        except Exception as error:
            items=[];attempt={'tool':'instaloader' if platform=='instagram' else 'facebook-scraper','status':error_code(error),'posts':0}
        attempts.append(attempt)
        if items or attempt['status'] in STOP:return items,attempt['status'],attempts
    if (platform=='tiktok' and re.fullmatch(r'/@[A-Za-z0-9_.-]+/?',profile_path)) or (platform=='x' and x_profile(source)):
        from public_embeds import collect as embed
        try:
            items=await asyncio.wait_for(asyncio.to_thread(embed,source,limit),timeout=18)
            count=len(items)
            items=candidate_audit.windowed(items)
            status='ok' if items else 'stale_feed_only' if count else 'no_public_posts'
        except Exception as error:status=error_code(error)
        attempts.append({'tool':'public_embed','status':status,'posts':len(items)})
        if items or status.startswith('robots_') or status in ('http_401','http_403','http_429','login_required','challenge_required'):return items,status,attempts
    if platform == 'tiktok' and os.environ.get('ARGUS_CANDIDATE_FEEDS') == '1':
        from candidate_feeds import feed
        from cloud_social import STOP
        try:
            items, attempt = await feed(source, min(limit, 100))
        except Exception as error:
            items, attempt = [], {'tool': 'tiktok-cli', 'status': error_code(error), 'posts': 0}
        attempts.append(attempt)
        if items or attempt['status'] in STOP:
            return items, attempt['status'], attempts
    if remaining_enabled and platform in ('tiktok', 'x'):
        from remaining_social import collect as remaining_collect, STOP
        items, extra_attempts = await remaining_collect(source, min(limit, 100))
        attempts.extend(extra_attempts)
        if items or attempts[-1]['status'] in STOP:
            return items, attempts[-1]['status'], attempts
    direct=source.get('platform') in ('telegram','rss','youtube','reddit','mailru','rutube')
    enhanced_web=os.environ.get('ARGUS_ENHANCED_COLLECTION')=='1' and platform in ('rss','mailru')
    try:
        if not source_valid(source):raise ValueError('invalid_source')
        if enhanced_web:
            from enhanced import collect as enhanced_collect,config
            items,diagnostic=await enhanced_collect(source,limit,config())
            attempts.append(diagnostic);status=diagnostic['status']
        elif direct:
            fn=telegram if source['platform']=='telegram' else rutube if source['platform']=='rutube' else feeds
            items=await asyncio.wait_for(asyncio.to_thread(fn,source,limit),timeout=50)
        elif platform=='facebook' and browser:
            # The public browser has already run before the HTTP adapters.
            return items,attempts[-1]['status'],attempts
        else:items=await asyncio.wait_for(social(browser,source,limit),timeout=depth.policy('browser')[2]+22)
        if not enhanced_web:status='ok' if items else 'no_public_posts'
    except Exception as error:status=error_code(error)
    if not enhanced_web or not attempts:
        attempts.append({'tool':'enhanced_web' if enhanced_web else 'public_feed_or_page' if direct else 'playwright','status':status,'posts':len(items)})
    if os.environ.get('ARGUS_ENHANCED_COLLECTION')=='1' and platform=='youtube' and not items and status=='no_public_posts':
        from rsshub_adapter import channel,collect as rsshub_collect
        from enhanced import config
        if channel(source):
            try:
                items=await asyncio.wait_for(asyncio.to_thread(rsshub_collect,source,limit,config()['rsshub_base']),timeout=20)
                status='ok' if items else 'no_public_posts'
            except Exception as error:status=error_code(error)
            attempts.append({'tool':'rsshub','status':status,'posts':len(items)})
    from enhanced import STOP
    if browser and not items and platform in ('rss','mailru') and status not in STOP|{'invalid_source'} and not status.startswith('robots_'):
        from browser_public import website
        try:
            recovered=await asyncio.wait_for(website(browser,source,limit),timeout=50)
            attempts.append({'tool':'playwright_article','status':'ok' if recovered else 'no_public_posts','posts':len(recovered)})
            if recovered:items=recovered;status='ok'
        except Exception as error:
            fallback_status=error_code(error);attempts.append({'tool':'playwright_article','status':fallback_status,'posts':0})
            if fallback_status in ('http_429','challenge_required'):status=fallback_status
    from media_metadata import eligible as media_eligible
    if media_fallback and not items and media_eligible(source) and status not in ('invalid_source','http_401','http_403','http_429','login_required','challenge_required'):
        from media_metadata import extract
        try:
            recovered,diagnostic=await extract(source,min(limit,5))
            attempts.append(diagnostic)
            if recovered:items=recovered;status='ok'
            elif diagnostic['status']=='rate_limited':status='http_429'
        except Exception as error:attempts.append({'tool':'yt-dlp','status':error_code(error),'posts':0})
    if platform=='x' and status in ('no_new_posts','no_public_posts') and any(a.get('status')=='stale_feed_only' for a in attempts):
        status='stale_feed_only'
    return items,status,attempts

def source_time_budget(args, platform):
    timeout=getattr(args,'source_timeout',165)
    if getattr(args,'stream_results',False):
        timeout=min(timeout,{'rss':45,'mailru':45,'telegram':35,'instagram':35,
                             'tiktok':40,'youtube':45,'facebook':40,'threads':40,
                             'vk':35,'ok':35,'whatsapp':20,'x':35,'rutube':65}.get(platform,65))
    return timeout


async def run(args):
    from playwright.async_api import async_playwright
    from runtime_policy import require_cloud
    require_cloud()
    from tinyfish_fetch import TinyFetch
    from cloud_social import isolate_platform_tokens
    from x_api import collect as collect_x_api, target as x_target
    x_bearer = os.environ.pop('ARGUS_X_BEARER_TOKEN', None)
    isolate_platform_tokens()
    actor_token = os.environ.pop('ARGUS_SOURCE_ACTOR_TOKEN', None)
    tinyfish = TinyFetch()
    if 'ARGUS_TINYFISH_FREE_URLS_PER_RUN' in os.environ:
        allowance=os.environ['ARGUS_TINYFISH_FREE_URLS_PER_RUN']
        total=min(12,int(allowance)) if re.fullmatch(r'[0-9]{1,2}',allowance) else 0
        # A profile lookup plus its verified details can need up to four URLs.
        offset=int(os.environ.get('GITHUB_RUN_NUMBER','0'))%args.shard_count
        position=(args.shard_index-offset)%args.shard_count
        tinyfish.remaining=min(4,max(0,total-4*position)) if args.shard_count>1 else total
    start=time.monotonic();root=Path(args.output);root.mkdir(parents=True,exist_ok=True)
    catalog=json.loads(Path(args.sources).read_text(encoding='utf-8'))
    sources=catalog['sources']; results=[]; all_posts={}
    registry_hash=None;disabled=set()
    if args.registry:
        from registry import validate
        registry_bytes=Path(args.registry).read_bytes();registry=validate(json.loads(registry_bytes))
        registry_hash=hashlib.sha256(registry_bytes).hexdigest()
        args.posts=100 if getattr(args,'public_page',False) else min(args.posts,registry['execution']['max_posts'])
        args.media_fallback=args.media_fallback and registry['execution']['media_fallback']
        disabled={c['platform'] for c in registry['collectors'] if not c['enabled']}
    cursors={}; state_updates={}; polling={}
    adaptive=getattr(args,'adaptive',False)
    incremental=getattr(args,'incremental',False)
    import depth
    backfill=getattr(args,'backfill',False)
    depth.mode.set('backfill' if backfill else 'live')
    depth.bounded_poll.set(bool(incremental and getattr(args,'stream_results',False)))
    batch_time=dt.datetime.now(dt.timezone.utc)
    hours=0 if backfill else getattr(args,'window_hours',0)
    since=batch_time-dt.timedelta(hours=hours) if hours and not catalog.get('permalink_backfill') else None
    if getattr(args,'cursors',None):
        state=json.loads(Path(args.cursors).read_text())
        if state.get('schema')!='argus-collection-cursors-v1': raise ValueError('invalid_cursor_schema')
        cursors=state.get('history_sources' if backfill else 'incremental_sources' if incremental else 'sources',{})
        if incremental:
            # A verified daily Telegram head is a safe starting point. The
            # fast lane only needs to catch up from it, not re-read deep history.
            cursors={**{k:{**v,'before':None,'exhausted':True,'updated_at':None} for k,v in state.get('sources',{}).items()
                        if v.get('head_id')},**cursors}
        polling=state.get('history_polling' if backfill else 'incremental_polling' if incremental else 'polling',{})
    concurrency=getattr(args,'concurrency',None) or (4 if incremental else 3)
    semaphore=asyncio.Semaphore(concurrency)
    browser_gate=asyncio.Semaphore(1) if getattr(args,'stream_results',False) else asyncio.Semaphore(concurrency)
    # Sensitive platforms stay serial; bounded Telegram/YouTube polling admits
    # two readers with paced starts, within the shared global limit.
    paced={'x':3.0,'instagram':3.0,'tiktok':3.0,'facebook':1.0,'vk':2.0,'ok':2.0,'threads':3.0,'telegram':1.0}
    if incremental:paced['youtube']=1.0
    def request_group(source):
        if source['platform'] in ('rss','mailru'):
            return urllib.parse.urlsplit(source['url']).hostname
        return source['platform']
    for source in sources:
        if source['platform'] in ('rss','mailru') and request_group(source):
            paced[request_group(source)]=1.0
    from source_admission import Admission
    admission=Admission(paced,{'telegram':2,'youtube':2} if depth.bounded_poll.get() else {})
    paused_platforms=set(catalog.get('cooling_platforms',[]))
    x_reads = {}
    head_cache={}
    head_errors={}
    from browser_pool import LazyRuntime
    async with LazyRuntime(async_playwright) as playwright:
        import shutil
        from browser_pool import LazyBrowser
        browser=LazyBrowser(playwright.chromium,executable_path=shutil.which('google-chrome') if incremental or backfill else None,
                            contexts=1 if os.getenv('ARGUS_COLLECTOR_RUNTIME')=='hostless' else concurrency)
        streamed=set()
        def stream_result(source):
            if not getattr(args,'stream_results',False) or source['id'] in streamed:return
            records=[r for r in results if r['source_id']==source['id']]
            if not records:return
            record=records[-1]
            keys=set(record['canonical_keys'])
            payload={'token':source['_lease_token'],'record':record,
                     'posts':[all_posts[k] for k in keys if k in all_posts],
                     'state_update':state_updates.get(source['id'])}
            spool=root/'spool';spool.mkdir(exist_ok=True)
            name=hashlib.sha256(source['id'].encode()).hexdigest()
            temporary=spool/(name+'.tmp')
            temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
            temporary.replace(spool/(name+'.json'))
            streamed.add(source['id'])
            # Once exported, parent persists this result before acknowledging a
            # checkpoint. A crash before persistence leaves the lease retryable.
            for key in keys:all_posts.pop(key,None)
        def health(source):
            previous=polling.get(source['id'],{})
            return previous if previous.get('url')==source['url'] else {}

        def skipped(source,status):
            results.append({'source_id':source['id'],'name':source.get('name',''),'url':source['url'],
                            'platform':source['platform'],'status':status,'posts':0,'seconds':0,
                            'canonical_keys':[],'attempts':[],'full_history_verified':False})

        started_sources=[]
        started_at={}
        async def collect_one(source):
            started_sources.append(source['id'])
            started_at[source['id']]=now()
            from source_health import retry_hints
            hints=retry_hints.get()
            page_budget,post_budget,seconds=depth.policy(source['platform'])
            if incremental and health(source).get('remaining_ranges', 0):
                page_budget = min(2 * page_budget, depth.CONFIG['live']['target_max_pages_per_source'])
            cursor=cursors.get(source['id'],{})
            if (depth.bounded_poll.get() and source['platform']=='telegram' and
                    (cursor.get('url')!=source['url'] or not cursor.get('head_id'))):
                # Publish the first verified head before attempting older pages.
                # Its checkpoint retains the continuation for subsequent passes.
                page_budget=1
            latest=health(source).get('last_post_published_at')
            if (incremental and cursor.get('url')==source['url'] and cursor.get('sitemap_next') and latest
                    and dt.timedelta(0)<=batch_time-instant(latest)<=dt.timedelta(minutes=30)):
                page_budget=min(depth.CONFIG['live']['target_max_pages_per_source'],max(page_budget,3*depth.CONFIG['platforms']['rss']['live_pages']))
            if depth.bounded_poll.get():
                page_budget=min(page_budget,depth.policy(source['platform'])[0])
            progress={'resume':cursor.get('next_url') if cursor.get('url')==source['url'] else None,
                      'facebook_pending':cursor.get('pending',[]) if cursor.get('url')==source['url'] else [],
                      'date_probes':cursor.get('date_probes',{}) if cursor.get('url')==source['url'] else {}}
            depth.browser_progress.set(progress if incremental or backfill else None)
            normalization=candidate_audit.start()
            rejected=[]
            candidate_audit.rejections.set(rejected)
            candidate_audit.publication_window.set(since)
            primary_result=x_reads.get(source['id'])
            begin=time.monotonic()-(primary_result[1]['primary_seconds'] if primary_result else 0)
            group=request_group(source)
            can_request=group not in paused_platforms
            use_x_api = bool(x_bearer and (incremental or backfill) and x_target(source))
            if not can_request:
                items,status,attempts=[],'deferred_rate_limit',[]
                if source['id'] in head_errors:
                    status=head_errors[source['id']]
                    attempts=[{'tool':'telegram_public_head','status':status,'posts':0}]
                elif source['id'] in head_cache:
                    items=telegram_html(source,100,head_cache[source['id']][0])
                    attempts=[{'tool':'telegram_public_head','status':'ok','posts':len(items)}]
                if primary_result:
                    from source_adapters import WALL
                    items,attempt=primary_result
                    attempts=[attempt]
                    if items or attempt['status'] in WALL:
                        status=attempt['status']
            elif use_x_api:
                first_since=since
                last_success=health(source).get('last_success_at')
                if since and last_success:
                    first_since=min(since,instant(last_success))
                items,status,attempts,update = await asyncio.to_thread(
                    collect_x_api, source, cursor if cursor.get('url') == source['url'] else {}, x_bearer,
                    max_pages=page_budget, max_posts=post_budget, seconds=seconds, since=first_since)
                if update:
                    state_updates[source['id']] = {**update, 'based_on':cursor.get('updated_at')}
            elif source['platform']=='telegram' and getattr(args,'telegram_history',False):
                from telegram_history import collect as collect_history
                try:
                    if not source_valid(source): raise ValueError('invalid_source')
                    cursor=cursors.get(source['id'],{})
                    native = (lambda s,body,limit: telegram_html(s,limit,body)) if incremental or backfill else None
                    def read_page(url):
                        if '?' not in url and source['id'] in head_errors:raise ValueError(head_errors[source['id']])
                        if '?' not in url and source['id'] in head_cache:return head_cache[source['id']]
                        if depth.bounded_poll.get():
                            # Finish history before the outer deadline so an unavailable
                            # later page cannot discard already parsed publications.
                            remaining=min(seconds,source_time_budget(args,'telegram')-10)-(time.monotonic()-begin)
                            if remaining<=0:raise TimeoutError('telegram_history_budget')
                            # Pool admission and the HTTP request have separate waits.
                            return fetch(url,timeout=max(.01,min(12,remaining/2)))
                        return fetch(url)
                    items,status,attempts,update=await asyncio.to_thread(collect_history,source,cursor if cursor.get('url')==source['url'] else None,
                        parser=native,fetcher=read_page,max_pages=page_budget,max_posts=post_budget,seconds=seconds,live=incremental)
                    if update: state_updates[source['id']]={**update,'based_on':cursor.get('updated_at')}
                except Exception as error:
                    status=error_code(error)
                    items,attempts=[],[{'tool':'telegram_public_history','status':status,'posts':0}]
            elif (incremental or backfill) and source['platform'] in ('rss','mailru'):
                from feed_history import collect as collect_feed
                cursor=cursors.get(source['id'],{})
                items,status,attempts,update=await asyncio.to_thread(collect_feed,source,cursor if cursor.get('url')==source['url'] else {},
                    window_hours=0 if backfill else hours or 48,max_pages=page_budget,max_posts=post_budget,seconds=seconds)
                if update: state_updates[source['id']]={**update,'based_on':cursor.get('updated_at')}
            elif (incremental or backfill) and source['platform']=='youtube':
                from youtube_history import collect as collect_youtube
                cursor=cursors.get(source['id'],{})
                items,status,attempts,update=await asyncio.to_thread(collect_youtube,source,cursor if cursor.get('url')==source['url'] else {},
                    backfill=backfill,details=2 if depth.bounded_poll.get() else depth.CONFIG['platforms']['youtube'][depth.mode.get()+'_details'],
                    seconds=seconds,reuse_channel=depth.bounded_poll.get())
                if update:state_updates[source['id']]={**update,'based_on':cursor.get('updated_at')}
            else:
                items,status,attempts=await collect_source(browser,source,args.posts,args.media_fallback,actor_token=actor_token,primary_result=primary_result)
                for wall in ('http_401','http_403','http_429','challenge_required','login_required','timeout',
                             'unexpected_destination','http_503','http_502','http_504','collection_failed'):
                    if normalization.get('browser_stop_'+wall):
                        status=wall
                        break
            if can_request and not use_x_api and os.environ.get('ARGUS_FACEBOOK_BROWSER_ONLY') != 'true':
                try:
                    async with asyncio.timeout(max(.01,150-(time.monotonic()-begin))):
                        items,status,attempts = await tinyfish.recover(source,args.posts,items,status,attempts)
                except Exception as error:
                    attempts.append({'tool':'tinyfish_fetch','status':error_code(error),'posts':0})
            if can_request and not use_x_api and items and status=='ok':
                try:
                    async with asyncio.timeout(max(.01,155-(time.monotonic()-begin))):
                        if os.environ.get('ARGUS_FACEBOOK_X') == '1' and source['platform'] in ('facebook','x'):
                            from facebook_x import enrich_one
                            await enrich_one(source,items,attempts)
                        if os.getenv('ARGUS_SOURCE_ADAPTERS') == '1':
                            from source_adapters import WALL, enrich_detail, enrich_media
                            if not any(a['status'] in WALL for a in attempts):
                                await enrich_detail(source,items,attempts,actor_token)
                            if args.media_fallback and os.getenv('ARGUS_COBALT_READY')=='1' and not any(a['status'] in WALL for a in attempts):
                                await enrich_media(items,attempts)
                except Exception as error:
                    attempts.append({'tool':'detail_enrichment','posts':0,'status':error_code(error)})
            metric_state={k:cursor[k] for k in ('metrics_checked_at','metrics_offset') if k in cursor and cursor.get('url')==source['url']}
            metrics_fit=(not getattr(args,'stream_results',False) or
                         time.monotonic()-begin+10 < source_time_budget(args,source['platform']))
            if (incremental or backfill) and can_request and items and status in ('ok','no_new_posts') and time.monotonic()-begin < (20 if depth.bounded_poll.get() else 45) and metrics_fit:
                from post_metrics import refresh,bounded_refresh
                from source_adapters import WALL
                if not any(a.get('status') in WALL for a in attempts):
                    if depth.bounded_poll.get():
                        items,refreshed=await bounded_refresh(source,items,cursor if cursor.get('url')==source['url'] else {})
                    else:refreshed=await asyncio.to_thread(refresh,source,items,cursor if cursor.get('url')==source['url'] else {})
                    if refreshed:
                        metric_attempt,metric_state=refreshed
                        attempts.append(metric_attempt)
            if source['platform']=='x' and status=='http_503' and (hints or any(a.get('retry_after') for a in attempts)):
                paused_platforms.add('x')
            if (status in ('http_429','rate_limited') or any(a.get('status') in ('http_429','rate_limited') for a in attempts)) and (incremental or any(os.environ.get(flag)=='1' for flag in ('ARGUS_SOURCE_ADAPTERS','ARGUS_FACEBOOK_X','ARGUS_SOCIAL_COLLECTION','ARGUS_REMAINING_COLLECTION','ARGUS_EMBED_COLLECTION'))) and group in paced:
                paused_platforms.add(group)
            if (not items and source['platform']=='tiktok' and
                    any(r.get('kind')=='publisher_embed' for r in source.get('references',[]))):
                from public_embeds import publisher_tiktok
                try:
                    async with asyncio.timeout(max(.01,155-(time.monotonic()-begin))):
                        recovered,attempt=await asyncio.to_thread(publisher_tiktok,source,args.posts)
                    attempts.append(attempt)
                    items=recovered
                    if items and status=='deferred_rate_limit':
                        status='publisher_only'
                except Exception as error:
                    attempts.append({'tool':'publisher_tiktok_embed','status':error_code(error),'posts':0})
            if hints and status=='http_503':
                paused_platforms.add(group)
            checked_at=now()
            if 'next_url' in progress and source['id'] not in state_updates and (items or progress['next_url']):
                state_updates[source['id']]={**({'x_api':cursor['x_api']} if cursor.get('url')==source['url'] and cursor.get('x_api') else {}),
                                             'url':source['url'],'next_url':progress['next_url'],'updated_at':checked_at,
                                             'based_on':cursor.get('updated_at')}
            if source['platform'] in ('vk','ok') and progress['date_probes'] and items:
                update = state_updates.setdefault(source['id'],{**cursor,'url':source['url'],'updated_at':checked_at,
                                                                  'based_on':cursor.get('updated_at')})
                update['date_probes']=progress['date_probes']
            if 'facebook_remaining' in progress:
                state_updates[source['id']]={'url':source['url'],'pending':progress['facebook_remaining'],
                                             'updated_at':checked_at,'based_on':cursor.get('updated_at')}
            if metric_state:
                update=state_updates.setdefault(source['id'],{**cursor,'url':source['url'],'updated_at':checked_at,'based_on':cursor.get('updated_at')})
                update.update(metric_state)
            if since and not use_x_api:
                raw_count=len(items)
                items=candidate_audit.windowed(items)
                if raw_count!=len(items) and not incremental:
                    # The historical cursor belongs to unfiltered backfills.
                    state_updates.pop(source['id'],None)
                if not items and status in ('ok','no_public_posts','no_new_posts') and any(
                        normalization.get(k) for k in ('before_publication_window','future_publication')):
                    status='no_new_posts' if source['platform'] in ('telegram','rss','youtube','mailru','reddit') else 'stale_feed_only'
            producing=[a for a in attempts if a.get('posts') and a.get('tool')]
            for item in items:
                engine=item.get('capture_mode') or (producing[0]['tool'] if producing else 'unknown')
                item.setdefault('tool',engine)
                owner=next((a for a in producing if a['tool'].replace('-','_')==engine.replace('-','_')),None)
                if owner is None and len(producing)==1:owner=producing[0]
                if owner is not None:
                    if item['canonical_key'] not in owner.setdefault('keys',[]):
                        owner['keys'].append(item['canonical_key'])
                    owner['full']=owner.get('full',0)+int(bool(item['text']) and item.get('text_may_be_truncated') is False)
                    if item.get('published_at'):
                        owner['dated']=owner.get('dated',0)+1
                        lag=max(0,(instant(checked_at)-instant(item['published_at'])).total_seconds()/3600)
                        owner['publication_lag_hours']=round(min(lag,owner.get('publication_lag_hours',lag)),2)
                key=item['canonical_key']
                all_posts[key]=merge_observations(all_posts.get(key),item)
            record={'source_id':str(source.get('id','')),'name':source.get('name',''),'url':source['url'],
                    'platform':source['platform'],'status':status,'posts':len(items),'seconds':round(time.monotonic()-begin,2),'attempts':attempts}
            latest = max((p for p in items if p.get('published_at') and not p.get('published_at_is_estimated')),
                         key=lambda p: instant(p['published_at']), default=None)
            record['latest_published_at'] = latest['published_at'] if latest else None
            if latest:
                record['latest_post_id'] = latest['canonical_key']
            record['canonical_keys']=sorted({p['canonical_key'] for p in items})
            record['collector_rows']=len(items)
            record['rejections']=rejected
            record['pre_window_rows']=raw_count if since and not use_x_api else len(items)
            if 'wall_pages' in progress: record['wall_pages'] = progress['wall_pages']
            # Feed history runs in a worker with its own crawl counters.
            for attempt in attempts:
                if attempt.get('tool') == 'telegram_public_history':
                    normalization['raw_input'] += attempt.get('raw_rows', 0)
                    for page in attempt.get('pages', []):
                        normalization.update(c['reason'] for c in page.get('candidates', []) if c['reason'])
                if attempt.get('tool') in ('public_feed_history','publisher_news_sitemap','pravaha'):
                    normalization['raw_input'] += attempt.get('raw_rows',0)
                    normalization.update(attempt.get('rejected_rows',{}))
            record['parser_inputs']=normalization.pop('raw_input',0)
            record['browser_observations']=normalization.pop('browser_observations',0)
            record['parser_exclusions']=dict(normalization)
            record['undated_posts']=sum(not p.get('published_at') for p in items)
            record['media_only_posts']=sum(not p['text'] and bool(p.get('media_urls') or p.get('media')) for p in items)
            record['checked_at']=checked_at
            record['started_at']=started_at[source['id']]
            hints.extend(a['retry_after'] for a in attempts if a.get('retry_after'))
            if hints:record['retry_after']=max(hints,key=instant)
            record['confirmed_dates']=sum(bool(p.get('published_at')) for p in items)
            record['full_texts']=sum(bool(p.get('text')) and p.get('text_may_be_truncated') is False for p in items)
            record['coverage']='public_history_batch' if source['platform']=='telegram' and getattr(args,'telegram_history',False) else 'available_page_only'
            checkpoint=state_updates.get(source['id'],cursor if cursor.get('url')==source['url'] else {})
            record['remaining_ranges']=len(checkpoint.get('listing',{}).get('pending',[])) + len(checkpoint.get('gaps',[])) + len(checkpoint.get('pending',[])) + int(bool(checkpoint.get('next_url'))) + int(bool(checkpoint.get('start'))) + int(bool(checkpoint.get('before') and not checkpoint.get('exhausted')))
            if use_x_api:
                record['coverage'] = 'x_api_v2'
                record['remaining_ranges'] = int(bool((checkpoint or cursor).get('x_api',{}).get('next_token')))
            record['status']=effective_status(record)
            if items and attempts and attempts[-1]['tool']=='tinyfish_fetch':
                record['coverage']='available_page_only'
            record['full_history_verified']=False
            if source['platform']=='facebook':
                record['coverage']='anonymous_feed'
                record['full_text_with_date']=sum(bool(p['text'] and not p.get('text_may_be_truncated',True) and p.get('published_at')) for p in items)
            results.append(record)
            print(json.dumps({k:record[k] for k in ('platform','status','posts','seconds')}),flush=True)
        async def one(source):
            platform=request_group(source)
            previous=health(source)
            reader_history.set(previous.get('engines',{}))
            async def bounded():
                from source_health import retry_hints
                from http_pool import request_metrics
                transport={'requests':0,'bytes':0,'seconds':0.0}
                request_metrics.set(transport)
                hints=[]
                retry_hints.set(hints)
                trace={'attempts':[]}
                depth.attempt_trace.set(trace)
                try:
                    async with semaphore:
                        if source['id'] not in x_reads and ((incremental and time.monotonic()-start>getattr(args,'batch_seconds',240)) or (adaptive and time.monotonic()-start>18*60)):
                            return skipped(source,'deferred_budget')
                        timeout=source_time_budget(args,source['platform'])
                        depth.source_deadline.set(time.monotonic()+timeout)
                        await asyncio.wait_for(collect_one(source),timeout=timeout)
                except Exception as exc:
                    state_updates.pop(source['id'],None)
                    status=error_code(exc)
                    if status=='http_429' or (status=='http_503' and hints):
                        paused_platforms.add(platform)
                    skipped(source,status)
                    results[-1].update(checked_at=now(),attempts=trace['attempts']+[
                        {'tool':'source_deadline','status':status,'posts':0,'error_type':type(exc).__name__}])
                    if source['id'] in started_at:
                        results[-1]['started_at']=started_at[source['id']]
                        results[-1]['seconds']=round((instant(results[-1]['checked_at'])-instant(started_at[source['id']])).total_seconds(),3)
                    if hints:results[-1]['retry_after']=max(hints,key=instant)
                finally:
                    for row in reversed(results):
                        if row['source_id']==source['id']:
                            row['pooled_http']={k:round(v,3) for k,v in transport.items()}
                            break
                    stream_result(source)
            async def admitted():
                # Waiting for the single browser must not occupy HTTP slots.
                if source['platform'] in {'facebook','threads','vk','ok','whatsapp'}:
                    async with browser_gate:return await bounded()
                return await bounded()
            async with admission.enter(platform):await admitted()
        selected=[];summaries=[]
        for platform in PLATFORMS:
            candidates=[s for s in sources if s.get('platform')==platform]
            active=[s for s in candidates if s.get('enabled',s.get('status','active')=='active')]
            eligible=active if args.sources_per_platform==0 else active[:args.sources_per_platform]
            chosen=[s for s in eligible if int(hashlib.sha256(s['id'].encode()).hexdigest(),16)%args.shard_count==args.shard_index];selected+=chosen
            summaries.append({'platform':platform,'configured':len(candidates),'enabled':len(active),'selected':len(chosen),
                              'deferred':max(0,len(active)-len(chosen)),'disabled':len(candidates)-len(active)})
        # Queue overdue sources first within each platform; interleave platforms at admission.
        from itertools import zip_longest
        groups=[[s for s in selected if s['platform']==p] for p in PLATFORMS]
        for group in groups:group.sort(key=lambda s:health(s).get('next_check_at',''))
        ready=[]
        for batch in zip_longest(*groups):
            for source in batch:
                if source is None:
                    continue
                previous=health(source)
                if source['platform'] in disabled:
                    skipped(source,'disabled_by_registry')
                elif adaptive and previous.get('next_check_at') and instant(previous['next_check_at'])>batch_time:
                    skipped(source,'not_due')
                else:
                    ready.append(source)
        if incremental and catalog.get('live_order'):
            priority={sid:i for i,sid in enumerate(catalog['live_order'])}
            ready.sort(key=lambda source:priority.get(source['id'],len(priority)))
            from news_order import telegram_head,pending_news
            signals=catalog.setdefault('news_signals',{})
            # Sample a small number of public heads only when no recent archive
            # signal exists. Reuse the page even if it contained no fresh news.
            if not signals and catalog.get('live_lane') != 'feeds' and not getattr(args,'stream_results',False):
                probes=[s for s in ready if s['platform']=='telegram' and source_valid(s)][:3]
                for i,source in enumerate(probes):
                    if i:await asyncio.sleep(paced['telegram'])
                    try:
                        observation,posts=await asyncio.to_thread(telegram_head,source)
                        head_cache[source['id']]=observation
                        signals.update(pending_news([{**p,'id':p['canonical_key']} for p in posts],polling,dt.datetime.now(dt.timezone.utc)))
                    except Exception as error:
                        code=error_code(error);head_errors[source['id']]=code
                        if code=='http_429':
                            paused_platforms.add('telegram')
                            break
            if signals:
                ready.sort(key=lambda s:(0,instant(signals[s['id']]['published_at'])) if s['id'] in signals else (1,priority[s['id']]))
                catalog['live_order']=[s['id'] for s in ready]
        async def collect_x_batch(group):
            from source_adapters import prime_x_sources
            async with semaphore:
                prepared, throttled = await prime_x_sources(group, args.posts, paced['x'])
            x_reads.update(prepared)
            if prepared:
                admission.last['x'] = time.monotonic()
            if throttled:
                paused_platforms.add('x')
            for source in group:
                await one(source)

        batch_x = not (x_bearer and (incremental or backfill)) and 'x' not in disabled and 'x' not in paused_platforms and os.getenv('ARGUS_SOURCE_ADAPTERS') == '1'
        x_group = [s for s in ready if batch_x and s['platform'] == 'x']
        from platform_workers import run_workers
        jobs = [run_workers([s for s in ready if not batch_x or s['platform'] != 'x'],one,
            {'telegram':2,'youtube':2,'rss':concurrency,'mailru':min(2,concurrency)})]
        if x_group:
            jobs.append(collect_x_batch(x_group))
        try:
            outcomes=await asyncio.gather(*jobs,return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome,BaseException):raise outcome
        finally:
            await browser.close()
    for summary in summaries:
        records=[r for r in results if r['platform']==summary['platform']]
        summary['posts']=sum(r['posts'] for r in records)
        summary['results']=dict(collections.Counter(r['status'] for r in records))
        summary['status']=platform_status(summary['configured'],summary['results'],summary['posts'])
    report={'format':'argus-collection-report-v1','run_id':os.environ.get('ARGUS_RUN_ID') or os.environ.get('GITHUB_RUN_ID',''),
            'updated_at':now(),'wall_seconds':round(time.monotonic()-start,2),'media_mode':'links_only',
            'catalog_provenance':catalog.get('provenance','unknown'),'catalog_sha256':hashlib.sha256(Path(args.sources).read_bytes()).hexdigest(),
            'shard_index':args.shard_index,'shard_count':args.shard_count,'platforms':summaries,'sources':results,'registry_sha256':registry_hash,
            'state_updates':state_updates,'public_page_mode':bool(getattr(args,'public_page',False))}
    revision=os.environ.get('ARGUS_COLLECTOR_REVISION','')
    if re.fullmatch('[0-9a-f]{40}',revision):report['collector_revision']=revision
    if incremental:
        report.update(incremental=True,live_lane=catalog.get('live_lane'),live_planned_at=catalog.get('live_planned_at'),live_order=catalog.get('live_order',[]),started_sources=started_sources,
                      news_signals=catalog.get('news_signals',{}),publisher_alternatives=catalog.get('publisher_alternatives',{}))
    if backfill: report['backfill'] = True
    if since: report['publication_window']={'hours':hours,'since':since.isoformat(),'until':report['updated_at'],'undated':'retain_separately'}
    if catalog.get('permalink_backfill'): report['permalink_backfill'] = True
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if getattr(args,'stream_results',False):return
    (root/'sources.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
    posts=root/'posts.jsonl';posts.write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in all_posts.values()),encoding='utf-8')
    media=root/'media';media.mkdir(exist_ok=True)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'local-storage'))
    from storage import pack
    pack(posts,media,root/'package.zip')
    print(json.dumps({'total_posts':len(all_posts),'platforms':summaries}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--sources',required=True);parser.add_argument('--output',default='collection/output')
    parser.add_argument('--sources-per-platform',type=int,default=10);parser.add_argument('--posts',type=int,default=10)
    parser.add_argument('--shard-index',type=int,default=0);parser.add_argument('--shard-count',type=int,default=1)
    parser.add_argument('--media-fallback',action='store_true')
    parser.add_argument('--registry')
    parser.add_argument('--public-page',action='store_true')
    parser.add_argument('--telegram-history',action='store_true')
    parser.add_argument('--cursors')
    parser.add_argument('--adaptive',action='store_true')
    parser.add_argument('--incremental',action='store_true')
    parser.add_argument('--backfill',action='store_true')
    parser.add_argument('--stream-results',action='store_true')
    parser.add_argument('--batch-seconds',type=int,default=240)
    parser.add_argument('--source-timeout',type=int,default=165)
    parser.add_argument('--concurrency',type=int,choices=range(1,7))
    parser.add_argument('--window-hours',type=int,choices=range(0,169),metavar='0..168',default=0)
    args=parser.parse_args()
    if not 0<=args.sources_per_platform<=100 or not 1<=args.posts<=30 or not 0<=args.shard_index<args.shard_count<=32: parser.error('Bounds exceeded')
    asyncio.run(run(args))
