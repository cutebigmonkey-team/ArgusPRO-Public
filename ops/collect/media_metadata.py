"""Bounded yt-dlp metadata extraction, without account sessions or media downloads."""
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time
from urllib.parse import urlsplit

SUPPORTED=('youtube','rutube','tiktok','vk','ok','instagram')


def eligible(source):
    platform=source.get('platform');path=urlsplit(source.get('url','')).path
    if platform in ('youtube','rutube','tiktok'):return True
    if platform=='vk':return bool(re.fullmatch(r'/(?:video|clip)-?\d+_\d+/?',path))
    if platform=='ok':return bool(re.fullmatch(r'/video/\d+/?',path))
    if platform=='instagram':return bool(re.fullmatch(r'/(?:p|reel)/[A-Za-z0-9_-]+/?',path))
    return False

def failure(text):
    text=text.lower()
    for pattern,code in [(r'429|too many requests','rate_limited'),
                         (r'sign in|login|log in|not a bot','login_required'),(r'403|forbidden','http_403'),
                         (r'unsupported url','unsupported_url'),(r'404|not found|unavailable|private video','unavailable')]:
        if re.search(pattern,text):return code
    return 'extraction_failed'

def normalize(source,info,limit):
    from run import post,url_ok
    items={}
    def walk(row,depth=0):
        if not isinstance(row,dict) or depth>3 or len(items)>=limit:return
        if 'entries' in row:
            for entry in (row.get('entries') or [])[:limit]:walk(entry,depth+1)
            return
        platform=source['platform'];identity=str(row.get('id',''))
        if not row.get('title'):return
        url=row.get('webpage_url') or row.get('url') or ''
        if platform=='youtube':
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}',identity):return
            if url_ok(url) and urlsplit(url).hostname not in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be'):return
            url='https://www.youtube.com/watch?v='+identity;key='youtube:'+identity
        elif platform=='rutube':
            if not re.fullmatch(r'[a-fA-F0-9]{32}',identity):return
            if not url_ok(url) or urlsplit(url).hostname not in ('rutube.ru','www.rutube.ru'):return
            # JSON-LD already archives live streams under this canonical video URL.
            url='https://rutube.ru/video/'+identity.lower()+'/'
            key='rutube:'+hashlib.sha256(url.encode()).hexdigest()
        elif platform=='tiktok':
            if not re.fullmatch(r'\d{8,}',identity) or not url_ok(url):return
            if urlsplit(url).hostname not in ('tiktok.com','www.tiktok.com'):return
            match=re.fullmatch(r'/@([A-Za-z0-9_.-]+)/video/(\d{8,})/?',urlsplit(url).path)
            if not match or match[2]!=identity:return
            source_handle=re.match(r'/@([A-Za-z0-9_.-]+)',urlsplit(source['url']).path)
            if source_handle and match[1].lower()!=source_handle[1].lower():return
            url='https://www.tiktok.com/@'+match[1]+'/video/'+identity;key='tiktok:'+identity
        elif platform in ('vk','ok','instagram'):
            if not eligible(source) or not url_ok(url):return
            from run import HOSTS
            if urlsplit(url).hostname not in HOSTS[platform]:return
            pattern={'vk':r'/(?:video|clip)(-?\d+_\d+)/?','ok':r'/video/(\d+)/?',
                     'instagram':r'/(?:p|reel)/([A-Za-z0-9_-]+)/?'}[platform]
            source_match=re.fullmatch(pattern,urlsplit(source['url']).path)
            match=re.fullmatch(pattern,urlsplit(url).path)
            if not match or match[1]!=source_match[1]:return
            identity=match[1]
            if platform=='instagram':url='https://www.instagram.com/p/'+identity+'/';key='instagram:'+identity
            elif platform=='vk':url='https://vk.com/video'+identity;key='vk:video'+identity
            else:url='https://ok.ru/video/'+identity;key='ok:video'+identity
        else:return
        stamp=None
        if isinstance(row.get('timestamp'),(int,float)):
            try:stamp=dt.datetime.fromtimestamp(row['timestamp'],dt.timezone.utc).isoformat()
            except (ValueError,OverflowError,OSError):pass
        text='\n\n'.join(dict.fromkeys(str(v) for v in (row['title'],row.get('description')) if v))
        thumbs=[t.get('url','') for t in row.get('thumbnails',[]) if isinstance(t,dict)]
        item=post(source,key,url,text,stamp,thumbs)
        from engagement_metrics import retain_counters
        retain_counters(item,row)
        item['capture_mode']='yt_dlp_metadata';item['text_may_be_truncated']=not bool(row.get('description'))
        # Language names only; signed subtitle/media URLs and request headers are not exported.
        item['subtitle_languages']=sorted(set((row.get('subtitles') or {}).keys()))[:100]
        item['automatic_caption_languages']=sorted(set((row.get('automatic_captions') or {}).keys()))[:100]
        items[key]=item
    walk(info)
    return list(items.values())

async def extract(source,limit=5,timeout=45,flat=True):
    from run import source_valid
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('Metadata collection requires a GitHub-hosted runner')
    if source.get('platform') not in SUPPORTED or not eligible(source) or not source_valid(source) or not 1<=limit<=5:
        raise ValueError('invalid_media_source')
    command=[sys.executable,'-m','yt_dlp','--ignore-config','--no-plugin-dirs','--no-cache-dir',
             '--proxy','','--skip-download','--dump-single-json','--ignore-no-formats-error',
             '--playlist-end',str(limit),'--socket-timeout','10','--retries','0','--extractor-retries','0',
             '--no-warnings','--use-extractors','youtube.*,rutube.*,tiktok,tiktok:user,vk,odnoklassniki,instagram','--js-runtimes','node']
    if flat:command.append('--flat-playlist')
    command+=['--',source['url']]
    start=time.monotonic();status=None
    with tempfile.TemporaryDirectory(prefix='argus-media-') as temp:
        root=Path(temp);out=root/'out.json';err=root/'err.txt';timing=root/'time.txt'
        with out.open('wb') as stdout,err.open('wb') as stderr:
            from cloud_social import worker_env
            process=await asyncio.create_subprocess_exec('/usr/bin/time','-o',str(timing),'-f',
                'user_seconds=%U\nsystem_seconds=%S\nmax_process_rss_kib=%M',*command,
                stdout=stdout,stderr=stderr,stdin=asyncio.subprocess.DEVNULL,start_new_session=True,env=worker_env())
            try:
                while process.returncode is None:
                    if time.monotonic()-start>timeout:status='timeout';break
                    if out.stat().st_size>8*1024**2 or err.stat().st_size>1024**2:status='output_limit';break
                    await asyncio.sleep(.2)
            finally:
                if process.returncode is None:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                await process.wait()
        metrics={'wall_seconds':round(time.monotonic()-start,2),'metadata_bytes':out.stat().st_size,'exit_code':process.returncode}
        if timing.exists():
            for line in timing.read_text().splitlines():
                if re.fullmatch(r'(user_seconds|system_seconds|max_process_rss_kib)=\d+(\.\d+)?',line):
                    k,v=line.split('=');metrics[k]=float(v)
        items=[]
        if not status:
            try:items=normalize(source,json.loads(out.read_text(encoding='utf-8')),limit)
            except (ValueError,TypeError,AttributeError):pass
            status='ok' if items else failure(err.read_text(encoding='utf-8',errors='replace')) if process.returncode else 'no_public_posts'
        return items,{'tool':'yt-dlp','status':status,'posts':len(items),**metrics}
