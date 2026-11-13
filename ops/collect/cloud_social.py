"""Account-free upstream collectors, executed only on hosted runners."""
import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

STOP = {'http_401', 'http_403', 'http_429', 'login_required', 'challenge_required'}
MANIFEST = json.loads(Path(__file__).with_name('social-upstreams.json').read_text())


def require_cloud():
    from runtime_policy import require_cloud as check
    check()


def tool_root():
    return Path(os.environ.get('ARGUS_SOCIAL_TOOLS', '.social-tools')).resolve()


def diagnostic(tool, status, items):
    return {'tool': tool, 'status': status, 'posts': len(items),
            'upstream_commit': MANIFEST[tool]['commit']}


def telegram(source, limit):
    require_cloud()
    from run import fetch, telegram_html, post
    from tg_radar.parser import parse_tme_page
    channel = urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    if not re.fullmatch(r'[A-Za-z0-9_]{3,64}', channel):
        raise ValueError('invalid_channel')
    body, _ = fetch('https://t.me/s/' + channel)
    return telegram_page(source,body,limit)


def telegram_page(source,body,limit=100):
    from tg_radar.parser import parse_tme_page
    channel = urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    items = telegram_rows(source, limit, body, parse_tme_page)
    # FreshRSS's standalone parser adds polls, attachment captions and albums.
    # Both parsers consume one response; there is no second Telegram request.
    request = json.dumps({'html':body.decode('utf-8',errors='replace'),'channel':channel})
    result = subprocess.run(['php',str(Path(__file__).with_name('telegramchannels_runner.php')),
        str(tool_root()/'telegramchannels')],input=request,text=True,encoding='utf-8',
        capture_output=True,timeout=8,check=True)
    data = json.loads(result.stdout)
    items = telegram_enrich(source,items,data,limit)
    if os.environ.get('ARGUS_PUBLIC_SOCIAL_ADAPTERS') == '1' and items:
        from public_cloud_adapters import add_marketpulse_metrics
        add_marketpulse_metrics(items, body)
    return items


def telegram_enrich(source, items, data, limit):
    import datetime as dt
    from bs4 import BeautifulSoup
    from run import post, url_ok
    channel = urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    indexed = {p['canonical_key']:p for p in items}
    for row in (data or {}).get('posts',[]):
        url = row.get('link','')
        if not url_ok(url) or urlsplit(url).hostname != 't.me':
            continue
        match = re.fullmatch('/'+re.escape(channel)+r'/(\d+)',urlsplit(url).path,re.I)
        if not match:
            continue
        soup = BeautifulSoup(row.get('html',''),'html.parser')
        for tag in soup(['script','style']):
            tag.decompose()
        text = soup.get_text('\n',strip=True)
        media = [e.get('src','') for e in soup.select('img[src],video[src]')]
        key = 'telegram:'+channel.lower()+'/'+match[1]
        stamp = dt.datetime.fromtimestamp(row['date'],dt.timezone.utc).isoformat() if isinstance(row.get('date'),int) and row['date']>0 else None
        item = indexed.get(key) or post(source,key,url,text,stamp,media)
        if not text and not media and not item['text']:
            continue
        if not item.get('published_at') and stamp:
            item['published_at'] = stamp
        if len(text)>len(item['text']):
            item['text']=text[:100000]
        item['media_urls']=list(dict.fromkeys(item['media_urls']+[m for m in media if url_ok(m)]))[:12]
        from social_links import extract,validated_urls
        item['outbound_social_links']=validated_urls(item.get('outbound_social_links',[])+extract(str(soup),url))
        item['extraction_tools']=['tg-radar','TelegramChannels','argus-media']
        item['telegramchannels_commit']=MANIFEST['telegramchannels']['commit']
        indexed[key]=item
    return sorted(indexed.values(),key=lambda p:int(p['canonical_key'].rsplit('/',1)[1]))[-limit:]


def telegram_rows(source, limit, body, parser):
    from run import telegram_html, post
    channel = urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    # Retain Argus's media-only posts and original paragraph breaks. Radar adds
    # views, forward and mention metadata from the very same HTTP response.
    native = {p['canonical_key']: p for p in telegram_html(source, limit, body)}
    parsed = parser(body.decode('utf-8', errors='replace'), channel)
    for row in parsed.messages:
        if row.channel_username.lower() != channel.lower() or row.tg_msg_id <= 0:
            continue
        key = 'telegram:' + channel.lower() + '/' + str(row.tg_msg_id)
        item = native.get(key)
        if not item and not row.text.strip():
            continue
        if not item:
            item = post(source, key, 'https://t.me/' + channel + '/' + str(row.tg_msg_id), row.text,
                        row.posted_at.isoformat() if row.posted_at else None)
        item.update(capture_mode='tg_radar_public_html', views=row.views,
                    mentions=row.mentions, forwarded_from=row.forward_from,
                    collector_commit=MANIFEST['tg-radar']['commit'])
        native[key] = item
    return sorted(native.values(), key=lambda p: int(p['canonical_key'].rsplit('/', 1)[1]))[-limit:]


def threads_target(source):
    from run import source_valid
    if source.get('platform') != 'threads' or not source_valid(source):
        raise ValueError('invalid_source')
    path = urlsplit(source['url']).path
    match = re.fullmatch(r'/@([A-Za-z0-9_.]{1,64})(?:/post/([A-Za-z0-9_-]{1,64}))?/?', path)
    if not match:
        raise ValueError('unsupported_source_shape')
    return match[1], match[2]


def threads_rows(source, rows, limit, tool, commit=None):
    from run import post, url_ok
    owner, requested = threads_target(source)
    items = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get('permalink') or row.get('post_url') or ''
        if not url_ok(url) or urlsplit(url).hostname not in ('threads.com', 'www.threads.com', 'threads.net', 'www.threads.net'):
            continue
        match = re.fullmatch(r'/@([A-Za-z0-9_.]+)/post/([A-Za-z0-9_-]+)/?', urlsplit(url).path)
        if not match or match[1].lower() != owner.lower() or (requested and match[2] != requested):
            continue
        author = row.get('username') or row.get('author')
        if author and str(author).lstrip('@').lower() != owner.lower():
            continue
        text = row.get('text') or ''
        media = row.get('media_urls') or []
        if not isinstance(text, str) or not isinstance(media, list) or (not text.strip() and not media):
            continue
        key = 'threads:' + match[2]
        item = post(source, key, 'https://www.threads.com/@' + match[1] + '/post/' + match[2],
                    text, row.get('timestamp') or row.get('posted_at_iso'), media)
        # A missing/zero Go timestamp is not an actual publication date.
        if item['published_at'] and item['published_at'].startswith('0001-'):
            item['published_at'] = None
        item.update(capture_mode=tool.replace('-', '_'), collector_commit=commit or MANIFEST[tool]['commit'],
                    text_may_be_truncated=True)
        if tool == 'threads-crawler':
            item['published_at'] = None
        metrics = {k: row[k] for k in ('like_count','reply_count','repost_count','quote_count','view_count') if k in row}
        if metrics:
            item['engagement'] = metrics
        items[key] = item
        if len(items) >= limit:
            break
    return list(items.values())


_platform_tokens = ContextVar('platform_tokens', default={})


def isolate_platform_tokens():
    _platform_tokens.set({name:os.environ.pop(name,'') for name in ('ARGUS_MAX_BOT_TOKEN','ARGUS_VK_TOKEN')})


def platform_token(name):
    return _platform_tokens.get().get(name, os.environ.get(name,''))


def worker_env():
    return {k:v for k,v in os.environ.items() if k in
            ('PATH','HOME','LANG','LD_LIBRARY_PATH','PLAYWRIGHT_BROWSERS_PATH','GITHUB_ACTIONS','RUNNER_ENVIRONMENT','ARGUS_COLLECTOR_RUNTIME')}


async def process(command, root, output, timeout=50, extra_env=None):
    """Kill the entire process tree on deadline, cancellation, or output overflow."""
    require_cloud()
    # No inherited account/session/provider variables or archive credentials.
    env = worker_env()
    if extra_env:
        if set(extra_env) not in ({'ARGUS_VK_TOKEN'},{'ARGUS_MAX_BOT_TOKEN'}):
            raise ValueError('invalid_worker_environment')
        env.update(extra_env)
    env.update(XDG_CACHE_HOME=str(root / 'cache'), XDG_DATA_HOME=str(root / 'data'),
               APIFY_LOCAL_STORAGE_DIR=str(root / 'apify'), CRAWLEE_STORAGE_DIR=str(root / 'crawlee'))
    if command[0]=='node' or Path(command[0]).parent.parent.name == 'short-post-python':
        # Playwright's default installation path uses XDG_CACHE_HOME. Keep the
        # installed browser location when isolating the worker's other caches.
        cache=Path(os.environ.get('XDG_CACHE_HOME',str(Path.home()/'.cache')))
        env['PLAYWRIGHT_BROWSERS_PATH']=os.environ.get('PLAYWRIGHT_BROWSERS_PATH',str(cache/'ms-playwright'))
    stdout_path, stderr_path = root / 'stdout', root / 'stderr'
    status = None
    started = time.monotonic()
    with stdout_path.open('wb') as out, stderr_path.open('wb') as err:
        proc = await asyncio.create_subprocess_exec(*command, stdout=out, stderr=err,
            stdin=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
        try:
            while proc.returncode is None:
                if time.monotonic() - started > timeout:
                    status = 'timeout'; break
                if any(p.exists() and p.stat().st_size > 8 * 1024**2 for p in (stdout_path, stderr_path, output)):
                    status = 'output_limit'; break
                await asyncio.sleep(.2)
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await proc.wait()
    if not status and any(p.exists() and p.stat().st_size > 8 * 1024**2 for p in (stdout_path, stderr_path, output)):
        status = 'output_limit'
    return proc.returncode, status, stdout_path


async def threads(source, limit, tool='threads-agent'):
    require_cloud()
    owner, shortcode = threads_target(source)
    if not 1 <= limit <= 100:
        raise ValueError('invalid_limit')
    root = tool_root()
    with tempfile.TemporaryDirectory(prefix='argus-social-') as temp:
        work = Path(temp)
        output = work / 'result.json'
        if tool == 'threads-agent':
            command = [str(root / 'th'), 'post' if shortcode else 'feed',
                       'https://www.threads.com/@' + owner + '/post/' + shortcode if shortcode else '@' + owner,
                       '--output','jsonl','--limit',str(limit),'--retries','0','--timeout','12s',
                       '--delay','2s','--no-cache','--quiet','--lang','uk-UA']
        elif tool == 'threads-crawler':
            command = ['node', str(root / 'threads-crawler' / 'argus-runner.mjs'),
                       'https://www.threads.com' + urlsplit(source['url']).path, str(limit), str(output)]
        else:
            raise ValueError('invalid_tool')
        code, status, stdout = await process(command, work, output)
        if status:
            return [], diagnostic(tool, status, [])
        if code:
            status = {3:'unavailable', 4:'login_required', 5:'http_429', 6:'network_error'}.get(code, 'extraction_failed') if tool == 'threads-agent' else 'browser_failed'
            attempt=diagnostic(tool,status,[])
            if tool=='threads-crawler':
                # Only the credential-free worker's bounded diagnostic; URLs
                # and query strings are removed before the private report.
                lines=(work/'stderr').read_text(encoding='utf-8',errors='replace').splitlines()
                detail=' '.join(line.strip() for line in lines if re.search(r'Error:|ArgumentError|Expected|Unexpected|Cannot|Executable',line))[:600]
                attempt['error_detail']=re.sub(r'https?://\S+','<url>',detail)
                attempt['exit_code']=code
            return [], attempt
        try:
            if tool == 'threads-agent':
                rows = [json.loads(line) for line in stdout.read_text(encoding='utf-8').splitlines() if line.strip()]
                status = 'no_public_posts'
            else:
                result = json.loads(output.read_text(encoding='utf-8'))
                rows, status = result['rows'], result['status']
                if status not in STOP | {'ok','no_public_posts','http_404','browser_failed'}:
                    status = 'browser_failed'
            items = threads_rows(source, rows, limit, tool)
            attempt=diagnostic(tool, 'ok' if items else status if status != 'ok' else 'no_public_posts', items)
            attempt['raw_rows']=len(rows)
            if tool=='threads-crawler' and result.get('error_detail'):
                attempt['error_detail']=re.sub(r'https?://\S+','<url>',str(result['error_detail']))[:600]
            return items, attempt
        except (ValueError, KeyError, TypeError, OSError):
            return [], diagnostic(tool, 'invalid_output', [])


async def collect(source, limit):
    from run import error_code
    if source['platform'] == 'telegram':
        try:
            items = await asyncio.to_thread(telegram, source, limit)
            return items, [diagnostic('tg-radar', 'ok' if items else 'no_public_posts', items)]
        except Exception as error:
            return [], [diagnostic('tg-radar', error_code(error), [])]
    attempts = []
    chain = ['threads-agent', 'threads-crawler']
    if os.environ.get('ARGUS_CANDIDATE_FEEDS') == '1':
        chain.insert(1, 'threads-scraper')
    if os.environ.get('ARGUS_PUBLIC_SOCIAL_ADAPTERS') == '1' and re.fullmatch(r'/@[\w.]+/?', urlsplit(source['url']).path, re.ASCII):
        chain.insert(1, 'ckelepel')
    from source_health import preferred
    from time import monotonic
    for tool in preferred(chain):
        started = monotonic()
        try:
            if tool == 'ckelepel':
                from public_cloud_adapters import hosted_ckelepel
                items, attempt = await hosted_ckelepel(source, limit)
            elif tool == 'threads-scraper':
                from candidate_feeds import feed
                items, attempt = await feed(source, limit)
            else:
                items, attempt = await threads(source, limit, tool)
        except Exception as error:
            items, attempt = [], {'tool': tool, 'status': error_code(error), 'posts': 0}
        attempt['seconds'] = round(monotonic()-started,2)
        attempts.append(attempt)
        if items or attempt['status'] in STOP:
            return items, attempts
    return [], attempts


def library_rows(source, rows, limit, tool):
    from run import HOSTS, IDENTITIES, post, url_ok, social_url
    from browser_public import facebook_owned
    items={}; platform=source['platform'];target=urlsplit(source['url'])
    requested=re.search(IDENTITIES[platform],source['url'])
    for row in rows:
        if not isinstance(row,dict):continue
        url=row.get('url','')
        if not url_ok(url) or urlsplit(url).hostname not in HOSTS[platform]:continue
        identity=re.search(IDENTITIES[platform],url)
        if not identity or (requested and identity[1]!=requested[1]):continue
        if platform=='instagram':
            if not requested and str(row.get('owner','')).lower()!=target.path.strip('/').lower():continue
        elif not requested:
            owned={'ownerUrl':'https://www.facebook.com/'+str(row.get('owner') or ''),
                   'ownerId':row.get('owner_id')}
            if not facebook_owned(source,owned,url):continue
        text=row.get('text') or '';media=row.get('media_urls') or []
        if not isinstance(text,str) or not isinstance(media,list) or (not text.strip() and not media):continue
        key=platform+':'+identity[1]
        item=post(source,key,social_url(url),text,row.get('published_at'),media)
        from engagement_metrics import retain_counters
        retain_counters(item,row)
        item.update(capture_mode=tool.replace('-','_'),collector_commit=MANIFEST[tool]['commit'])
        items[key]=item
        if len(items)>=limit:break
    return list(items.values())


async def library(source,limit):
    from run import source_valid
    require_cloud()
    if not source_valid(source) or source['platform'] not in ('instagram','facebook'):
        raise ValueError('invalid_source')
    tool='instaloader' if source['platform']=='instagram' else 'facebook-scraper'
    with tempfile.TemporaryDirectory(prefix='argus-python-social-') as temp:
        root=Path(temp);output=root/'result.json'
        command=[str(tool_root()/'python/bin/python'),str(Path(__file__).with_name('python_social_worker.py')),
                 tool,source['url'],str(limit),str(output)]
        code,status,_=await process(command,root,output,35)
        if status or code:return [],diagnostic(tool,status or 'worker_failed',[])
        try:
            data=json.loads(output.read_text(encoding='utf-8'))
            items=library_rows(source,data['rows'],limit,tool)
            status=data['status']
            if status not in STOP|{'ok','no_public_posts','unavailable','timeout','extraction_failed','partial_results'}:status='invalid_output'
            outcome=status if status in STOP|{'partial_results'} else 'ok' if items else status if status!='ok' else 'no_public_posts'
            attempt=diagnostic(tool,outcome,items)
            if data.get('fetch_complete') is False:attempt['fetch_complete']=False
            reason=data.get('stop_reason')
            if reason in STOP|{'timeout','extraction_failed','unavailable'}:attempt['stop_reason']=reason
            return items,attempt
        except (ValueError,KeyError,TypeError,OSError):return [],diagnostic(tool,'invalid_output',[])
