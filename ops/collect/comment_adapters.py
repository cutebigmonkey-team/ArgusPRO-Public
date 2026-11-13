"""Bounded comment pages. Credentials are optional; missing access is explicit.

This child process receives post metadata, never Drive or database credentials.
Each call returns a resumable page, not a claim that all comments were collected.
"""
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
from urllib.parse import urlsplit, urljoin, urlencode, parse_qs
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup


def result(rows=(), status='success', reason='', cursor=None, retry_after=0):
    return dict(rows=list(rows), status=status, reason=reason, cursor=cursor, retry_after=retry_after)


def get(url, headers=None):
    from run import safe_url
    from http_pool import request
    safe_url(url)
    raw, _ = request(url, {'User-Agent':'ARGUS/1.0 public-comments', **(headers or {})}, 8)
    return json.loads(raw)


def youtube(post, cursor):
    key=os.getenv('ARGUS_YOUTUBE_API_KEY') or os.getenv('YOUTUBE_API_KEY')
    if not key:return youtube_public(post,cursor)
    u=urlsplit(post['url']); video=parse_qs(u.query).get('v',[''])[0]
    if u.hostname=='youtu.be':video=u.path.strip('/')
    if not video:
        match=re.search(r'/(?:shorts|embed)/([\w-]{11})',u.path);video=match[1] if match else ''
    if not re.fullmatch(r'[\w-]{11}',video):return result(status='unsupported',reason='video_id_missing')
    state=dict(cursor or {}); replies=list(state.get('replies',[]))
    params={'key':key,'part':'snippet','maxResults':100,'textFormat':'plainText'}
    if replies:
        parent=replies[0];params['parentId']=parent
        if state.get('reply_page'):params['pageToken']=state['reply_page']
        data=get('https://www.googleapis.com/youtube/v3/comments?'+urlencode(params))
        nodes=data['items']
        if data.get('nextPageToken'):state['reply_page']=data['nextPageToken']
        else:replies.pop(0);state.pop('reply_page',None)
        state['replies']=replies
        more=bool(replies or state.get('page'))
    else:
        params.update(videoId=video,order='time')
        if state.get('page'):params['pageToken']=state['page']
        data=get('https://www.googleapis.com/youtube/v3/commentThreads?'+urlencode(params))
        nodes=[r['snippet']['topLevelComment'] for r in data['items']]
        state={'page':data.get('nextPageToken'), 'replies':[r['snippet']['topLevelComment']['id'] for r in data['items'] if r['snippet'].get('totalReplyCount',0)]}
        more=bool(state['page'] or state['replies'])
    rows=[]
    for node in nodes:
        s=node['snippet']
        rows.append(dict(id=node['id'],text=s.get('textOriginal') or s.get('textDisplay',''),author=s.get('authorDisplayName'),
            parent_id=s.get('parentId'),published_at=s.get('publishedAt'),updated_at=s.get('updatedAt'),
            likes=s.get('likeCount'),url='https://www.youtube.com/watch?'+urlencode({'v':video,'lc':node['id']})))
    return result(rows,'partial' if more else 'success',cursor=state if more else None)


def youtube_public(post,cursor):
    """Read public comment continuations directly; no player/media extraction."""
    try:
        from youtube_comment_downloader import YoutubeCommentDownloader,SORT_BY_RECENT
    except ImportError:
        return youtube_ytdlp(post,cursor)
    u=urlsplit(post['url'])
    if u.hostname not in ('www.youtube.com','youtube.com','m.youtube.com','youtu.be'):
        return result(status='unsupported',reason='invalid_youtube_url')
    video=parse_qs(u.query).get('v',[''])[0]
    if u.hostname=='youtu.be':video=u.path.strip('/')
    if not video:
        match=re.search(r'/(?:shorts|embed)/([\w-]{11})',u.path);video=match[1] if match else ''
    if not re.fullmatch(r'[\w-]{11}',video):return result(status='unsupported',reason='video_id_missing')
    deadline=time.monotonic()+24
    class Reader(YoutubeCommentDownloader):
        def ajax_request(self,endpoint,ytcfg,**kwargs):
            return super().ajax_request(endpoint,ytcfg,retries=1,sleep=0,timeout=8)
    client=Reader();request=client.session.request
    def bounded_request(method,url,**kwargs):
        if urlsplit(url).hostname not in ('www.youtube.com','youtube.com','consent.youtube.com'):
            raise ValueError('unexpected_comment_endpoint')
        left=deadline-time.monotonic()
        if left<1:raise TimeoutError('comment_page_deadline')
        kwargs['timeout']=(min(3,left/2),min(5,left/2))
        response=request(method,url,**kwargs)
        if response.status_code>=400:
            raise urllib.error.HTTPError(url,response.status_code,'public_comment_http_error',response.headers,None)
        return response
    client.session.request=bounded_request
    rows=[]
    try:
        for c in client.get_comments(video,sort_by=SORT_BY_RECENT,sleep=0):
            identity=str(c.get('cid') or '')
            if not identity or not c.get('text'):continue
            votes=str(c.get('votes','')).replace(',','').replace(' ','')
            rows.append(dict(id=identity,parent_id=identity.split('.')[0] if c.get('reply') and '.' in identity else None,
                text=c['text'],author=c.get('author'),published_at=c.get('time_parsed'),published_at_precision='approximate',
                likes=int(votes) if votes.isdigit() else None,
                url='https://www.youtube.com/watch?'+urlencode({'v':video,'lc':identity})))
            if len(rows)>=200 or time.monotonic()>=deadline:break
    except urllib.error.HTTPError as error:
        if not rows:raise
        if error.code==429:
            try:retry=float(error.headers.get('Retry-After',0))
            except (ValueError,TypeError):
                try:retry=max(0,parsedate_to_datetime(error.headers['Retry-After']).timestamp()-time.time())
                except Exception:retry=0
            return result(rows,'rate_limited','http_429',retry_after=retry)
        return result(rows,'partial','youtube_public_interrupted')
    except Exception:
        if rows:return result(rows,'partial','youtube_public_interrupted')
        raise
    finally:client.session.close()
    return result(rows,'partial','youtube_public_bounded_200') if rows else result(status='failed',reason='youtube_comments_not_verified')


def youtube_ytdlp(post,cursor):
    """Use the existing yt-dlp dependency, without keys, cookies or video downloads."""
    import yt_dlp
    from yt_dlp.utils import DownloadError
    u=urlsplit(post['url'])
    if u.hostname not in ('www.youtube.com','youtube.com','m.youtube.com','youtu.be'):
        return result(status='unsupported',reason='invalid_youtube_url')
    messages=[]
    class Logger:
        def debug(self,message):pass
        def warning(self,message):messages.append(message.lower())
        def error(self,message):messages.append(message.lower())
    options={'skip_download':True,'getcomments':True,'ignore_no_formats_error':True,'quiet':True,'logger':Logger(),
        'socket_timeout':8,'retries':0,'extractor_retries':0,'noplaylist':True,'cachedir':False,
        'extractor_args':{'youtube':{'comment_sort':['new'],'max_comments':['200','150','50','10'],'skip':['dash','hls']}}}
    try:
        with yt_dlp.YoutubeDL(options) as client:data=client.extract_info(post['url'],download=False)
    except DownloadError:
        reason=' '.join(messages)
        status='auth_required' if any(v in reason for v in ('sign in','login','not a bot')) else 'rate_limited' if '429' in reason else 'forbidden' if '403' in reason else 'failed'
        return result(status=status,reason='youtube_public_'+status)
    rows=[]
    for c in (data or {}).get('comments') or []:
        rows.append(dict(id=c.get('id'),parent_id=c.get('parent') if c.get('parent')!='root' else None,
            text=c.get('text'),author=c.get('author'),published_at=c.get('timestamp'),likes=c.get('like_count'),
            url='https://www.youtube.com/watch?'+urlencode({'v':data['id'],'lc':c.get('id','')})))
    # yt-dlp may return only a bounded sample or stop on a warning. Do not claim completeness.
    if rows:return result(rows,'partial','youtube_public_bounded_200')
    if any('disabled' in m and 'comment' in m for m in messages):return result(status='success',reason='comments_disabled')
    if (data or {}).get('comment_count')==0:return result(status='success',reason='no_comments')
    return result(status='failed',reason='youtube_comments_not_verified')


def wordpress(post,cursor):
    from crawl_common import CrawlState
    crawl=CrawlState({'id':'comments','name':'Comments','url':post['url'],'platform':'rss','enabled':True},100,4)
    u=urlsplit(post['url']);origin=u.scheme+'://'+u.netloc
    state=dict(cursor or {})
    if not state.get('endpoint'):
        raw,final=crawl.read(post['url'])
        soup=BeautifulSoup(raw,'html.parser')
        link=soup.find('link',attrs={'rel':'alternate','type':'application/json'})
        endpoint=urljoin(final,link.get('href','')) if link else ''
        m=re.search(r'/wp-json/wp/v2/posts/(\d+)(?:\?|$)',endpoint)
        if not m or urlsplit(endpoint).netloc!=u.netloc:
            from comment_public import payloads,structured_comments
            rows=structured_comments(post,list(payloads(raw.decode('utf-8','replace'))))
            return result(rows,'partial','public_page_excerpt') if rows else result(status='unsupported',reason='public_comment_endpoint_not_found')
        state={'endpoint':origin+'/wp-json/wp/v2/comments','post':m[1],'page':1}
    if urlsplit(state['endpoint']).netloc!=u.netloc:return result(status='failed',reason='invalid_cursor')
    # Ascending order avoids shifting older pages whenever a new comment arrives.
    url=state['endpoint']+'?'+urlencode({'post':state['post'],'page':state['page'],'per_page':100,'order':'asc','orderby':'date','context':'view'})
    try:raw,_=crawl.read(url);data=json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code==400 and state['page']>1 and error.fp:
            try:code=json.loads(error.read(65536)).get('code')
            except (ValueError,AttributeError):code=None
            if code=='rest_comment_invalid_page_number':return result(reason='end_of_available_pages')
        raise
    if not isinstance(data,list):return result(status='failed',reason='unexpected_response')
    rows=[dict(id=str(r['id']),parent_id=str(r['parent']) if r.get('parent') else None,
        text=BeautifulSoup(r.get('content',{}).get('rendered',''),'html.parser').get_text(' ',strip=True),
        author=r.get('author_name'),url=r.get('link'),published_at=(r['date_gmt']+'Z') if r.get('date_gmt') else None) for r in data]
    more=len(data)==100
    return result(rows,'partial' if more else 'success',cursor={**state,'page':state['page']+1} if more else None)


def reddit(post,cursor):
    from crawl_common import CrawlState
    u=urlsplit(post['url'])
    if u.hostname not in ('www.reddit.com','reddit.com','old.reddit.com'):return result(status='unsupported',reason='invalid_reddit_url')
    url='https://www.reddit.com'+u.path.rstrip('/')+'.json?limit=100&sort=new&raw_json=1'
    crawl=CrawlState({'id':'comments','name':'Comments','url':url,'platform':'rss','enabled':True},100,4)
    raw,_=crawl.read(url);data=json.loads(raw);rows=[];more=False
    def walk(children):
        nonlocal more
        for child in children:
            if child.get('kind')=='more':more=True;continue
            if child.get('kind')!='t1':continue
            r=child['data'];parent=r.get('parent_id','')
            rows.append(dict(id=r['name'],parent_id=parent if parent.startswith('t1_') else None,
                text=r.get('body',''),author=r.get('author'),published_at=r.get('created_utc'),
                likes=r.get('ups'),url=urljoin('https://www.reddit.com',r.get('permalink',''))))
            nested=r.get('replies')
            if isinstance(nested,dict):walk(nested.get('data',{}).get('children',[]))
    walk(data[1]['data']['children'])
    return result(rows,'partial' if more else 'success','reddit_more_replies_unavailable' if more else '')


def telegram(post,cursor):
    required=('ARGUS_TELEGRAM_API_ID','ARGUS_TELEGRAM_API_HASH','ARGUS_TELEGRAM_SESSION')
    if any(not os.getenv(k) for k in required):
        from comment_public import telegram_public
        return telegram_public(post,cursor)
    match=re.search(r'^https://t\.me/(?:s/)?(\w+)/(\d+)',post['url'])
    if not match:return result(status='unsupported',reason='public_channel_post_required')
    async def run():
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.errors import FloodWaitError
        client=TelegramClient(StringSession(os.environ[required[2]]),int(os.environ[required[0]]),os.environ[required[1]],
            request_retries=0,connection_retries=0,flood_sleep_threshold=0,timeout=8)
        try:
            await client.connect()
            if not await client.is_user_authorized():return result(status='auth_required',reason='telegram_session_expired')
            rows=[];offset=int((cursor or {}).get('offset',0))
            async for msg in client.iter_messages(match[1],reply_to=int(match[2]),limit=100,offset_id=offset):
                parent=getattr(msg.reply_to,'reply_to_msg_id',None)
                root=getattr(msg.reply_to,'reply_to_top_id',None)
                # Discussion root IDs belong to the linked group, not the channel.
                parent=str(parent) if root and parent!=root else None
                sender=msg.sender
                name=' '.join(filter(None,[getattr(sender,'first_name',None),getattr(sender,'last_name',None)])) or getattr(sender,'title',None)
                rows.append(dict(id=str(msg.id),parent_id=parent,text=msg.message or '',author=name,
                    published_at=msg.date.isoformat() if msg.date else None,url=post['url']+'?comment='+str(msg.id)))
            more=len(rows)==100
            return result(rows,'partial' if more else 'success',cursor={'offset':int(rows[-1]['id'])} if more else None)
        except FloodWaitError as e:return result(status='rate_limited',reason='telegram_flood_wait',retry_after=e.seconds)
        finally:await client.disconnect()
    async def bounded():return await asyncio.wait_for(run(),timeout=25)
    return asyncio.run(bounded())


ADAPTERS={'youtube':youtube,'telegram':telegram,'reddit':reddit,'rss':wordpress,'wordpress':wordpress}


def social_payload(post,payload):
    """Accept comments only within a matching platform post object."""
    platform=post['platform'];rows=[]
    if platform=='facebook':
        from facebook_feed import extract,story_post
        from social_links import identity
        target=identity(post['url'])
        if not target:return rows
        source={'id':post.get('source_id','comments'),'name':post.get('source_name',''),'platform':'facebook','url':post['url']}
        for story in extract(payload,detail=True)[0]:
            parsed=story_post(source,story,required_key=target['canonical_key'])
            if parsed:rows.extend(parsed.get('public_comments',[]))
        return rows
    target=re.search(r'/(?:p|reel)/([\w-]+)',post['url']) if platform=='instagram' else re.search(r'/video/(\d+)',post['url'])
    if not target:return rows
    stack=[payload]
    while stack:
        value=stack.pop()
        if isinstance(value,list):stack.extend(value);continue
        if not isinstance(value,dict):continue
        if platform=='instagram' and value.get('shortcode')==target[1]:
            for field in ('edge_media_to_parent_comment','edge_media_to_comment'):
                for edge in (value.get(field) or {}).get('edges',[]):
                    c=edge.get('node',{});owner=c.get('owner') or {}
                    rows.append(dict(id=c.get('id'),text=c.get('text'),author=owner.get('username'),published_at=c.get('created_at')))
                    for reply in (c.get('edge_threaded_comments') or {}).get('edges',[]):
                        r=reply.get('node',{});rows.append(dict(id=r.get('id'),parent_id=c.get('id'),text=r.get('text'),author=(r.get('owner') or {}).get('username'),published_at=r.get('created_at')))
        elif platform=='tiktok' and str(value.get('aweme_id',''))==target[1] and value.get('cid') and isinstance(value.get('text'),str):
            rows.append(dict(id=value['cid'],text=value['text'],author=(value.get('user') or {}).get('nickname'),published_at=value.get('create_time'),likes=value.get('digg_count'),parent_id=value.get('reply_id') if str(value.get('reply_id','0'))!='0' else None))
        stack.extend(v for v in value.values() if isinstance(v,(dict,list)))
    return rows


def social_public(post,cursor):
    from run import fetch,HOSTS
    from browser_public import page_error
    if urlsplit(post['url']).hostname not in HOSTS[post['platform']]:return result(status='unsupported',reason='invalid_social_url')
    raw,final=fetch(post['url'],timeout=8)
    body=raw.decode('utf-8','replace');error=page_error(final,body)
    if error:return result(status='auth_required' if error=='login_required' else 'forbidden',reason=error)
    rows=[]
    for script in BeautifulSoup(body,'html.parser').find_all('script',type='application/json'):
        try:rows.extend(social_payload(post,json.loads(script.string or script.get_text())))
        except (ValueError,TypeError):continue
    if rows:return result(rows,'partial','public_page_excerpt')
    return result(status='partial' if post.get('public_comments') or post.get('instagram_comments') else 'auth_required',
        rows=post.get('public_comments') or post.get('instagram_comments') or [],reason='public_comments_not_exposed')


from comment_public import remaining_public
from comment_official import facebook,instagram,tiktok,threads,x,vk,ok
ADAPTERS.update(facebook=facebook,instagram=instagram,tiktok=tiktok,threads=threads,x=x,vk=vk,ok=ok,max=remaining_public)


def collect(post,cursor=None):
    platform=post.get('platform')
    adapter=ADAPTERS.get(platform)
    if not adapter:return result(status='unsupported',reason='comment_adapter_not_available')
    try:
        if cursor is not None and not isinstance(cursor,dict):return result(status='failed',reason='invalid_cursor')
        if platform in ('facebook','instagram','tiktok','threads','x','vk','ok'):
            from run import HOSTS
            u=urlsplit(post['url'])
            if u.scheme!='https' or u.hostname not in HOSTS[platform] or u.username or u.password or u.port:
                return result(status='unsupported',reason='invalid_social_url')
        return adapter(post,cursor)
    except urllib.error.HTTPError as e:
        retry=0
        headers=e.headers or {}
        try:retry=float(headers.get('Retry-After',0))
        except (ValueError,TypeError):
            try:retry=max(0,parsedate_to_datetime(headers['Retry-After']).timestamp()-time.time())
            except Exception:pass
        if e.code==429:
            try:retry=max(retry,float(headers.get('x-rate-limit-reset',0))-time.time())
            except (ValueError,TypeError):pass
        status={401:'auth_required',403:'forbidden',429:'rate_limited'}.get(e.code,'failed')
        if e.fp and platform in ('facebook','instagram','threads','youtube','tiktok','vk'):
            try:
                data=json.loads(e.read(65536))
                if platform=='youtube':
                    reasons={r.get('reason') for r in data.get('error',{}).get('errors',[])}
                    if 'commentsDisabled' in reasons:return result(reason='comments_disabled')
                    if reasons & {'quotaExceeded','dailyLimitExceeded','rateLimitExceeded','userRateLimitExceeded'}:
                        return result(status='rate_limited',reason='youtube_api_quota',retry_after=max(3600,retry))
                from comment_official import error_result
                failure=error_result(data)
                if failure:
                    failure['retry_after']=retry
                    return failure
            except (ValueError,TypeError,AttributeError):pass
        return result(status=status,reason='http_'+str(e.code),retry_after=retry)
    except ValueError as e:
        reason=str(e)
        return result(status='forbidden' if reason.startswith('robots_') else 'failed',reason=reason if reason.startswith('robots_') else 'invalid_response')
    except ImportError:return result(status='unsupported',reason='adapter_dependency_missing')
    except Exception as e:return result(status='failed',reason=type(e).__name__)


if __name__=='__main__':
    # Linux cloud deadline; supervisor additionally reaps abandoned children.
    if hasattr(__import__('signal'),'SIGALRM'):
        import signal
        signal.alarm(30)
    job=json.load(sys.stdin)
    print(json.dumps(collect(job['post'],job.get('cursor')),ensure_ascii=False))
