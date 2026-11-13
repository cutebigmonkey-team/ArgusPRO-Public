"""Keep the native Atom head and drain verified gaps in a channel's public uploads."""
import json
import hashlib
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs,urlsplit,urlencode
from bs4 import BeautifulSoup

from run import date, error_code, fetch, now, post
from source_health import instant
from youtube_feed import CHANNEL, parse, resolve

VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
PAGE = 60


def player_metadata(body):
    # Mobile pages initialize this variable to null before assigning the actual
    # player response. A null initializer is not missing/private video evidence.
    for match in re.finditer(rb'ytInitialPlayerResponse\s*=\s*',body):
        try:value=json.JSONDecoder().raw_decode(body[match.end():].decode('utf-8'))[0]
        except ValueError:continue
        if isinstance(value,dict):return value
    return {}


def capture_observation(source,url,reader=fetch):
    ids=parse_qs(urlsplit(url).query).get('v',[])
    if len(ids)!=1 or not VIDEO.fullmatch(ids[0]) or url!='https://www.youtube.com/watch?v='+ids[0]:
        raise ValueError('invalid_video_url')
    channel=resolve(source,reader)
    body,final=reader(url)
    if final!=url:raise ValueError('unexpected_video_redirect')
    player=player_metadata(body)
    details=player.get('videoDetails',{});meta=player.get('microformat',{}).get('playerMicroformatRenderer',{})
    if details.get('channelId')!=channel or details.get('videoId')!=ids[0] or not date(meta.get('publishDate')):
        raise ValueError('public_date_not_verified')
    return {'format':'youtube-watch-microformat-v1','url':url,'video_id':ids[0],'channel_id':channel,
            'publishDate':meta['publishDate'],'observed_at':now(),'response_sha256':hashlib.sha256(body).hexdigest()}


def watch(source, channel, video):
    if not VIDEO.fullmatch(video):raise ValueError('invalid_video_id')
    url='https://www.youtube.com/watch?v='+video
    body,final=fetch(url)
    if urlsplit(final).hostname not in ('www.youtube.com','youtube.com'):
        raise ValueError('unexpected_video_redirect')
    player=player_metadata(body)
    details=player.get('videoDetails',{})
    if player.get('playabilityStatus',{}).get('status')!='OK':
        # Playback may be restricted on a runner. Accept only metadata the
        # official, account-free oEmbed endpoint exposes for this public video.
        embed,_=fetch('https://www.youtube.com/oembed?'+urlencode({'url':url,'format':'json'}))
        info=json.loads(embed)
        frame=BeautifulSoup(info.get('html',''),'html.parser').find('iframe',src=True)
        frame_url=urlsplit(frame['src']) if frame else None
        author=urlsplit(info.get('author_url',''))
        if (not frame_url or frame_url.scheme!='https' or frame_url.hostname!='www.youtube.com' or
                frame_url.path!='/embed/'+video or info.get('type')!='video' or not info.get('title') or
                author.scheme!='https' or author.hostname!='www.youtube.com' or
                resolve({'url':author.geturl()},fetch)!=channel):
            raise ValueError('video_owner_mismatch')
        soup=BeautifulSoup(body,'html.parser')
        stamp=soup.select_one('head meta[itemprop="datePublished"][content], head meta[property="article:published_time"][content]')
        same_watch=(urlsplit(final).path=='/watch' and parse_qs(urlsplit(final).query).get('v')==[video])
        when=stamp['content'] if stamp and same_watch else None
        # The public watch document can expose the owner's microformat even
        # when the player declines playback. It must describe this exact card.
        if same_watch and details.get('videoId')==video and details.get('channelId')==channel:
            when=player.get('microformat',{}).get('playerMicroformatRenderer',{}).get('publishDate') or when
        result=post(source,'youtube:'+video,url,info['title'],when)
        from post_metrics import youtube_metrics
        from engagement_metrics import retain_counters
        retain_counters(result,youtube_metrics(body,video))
        result.update(capture_mode='youtube_public_oembed',text_may_be_truncated=True)
        return result
    if details.get('videoId')!=video or details.get('channelId')!=channel:
        raise ValueError('video_owner_mismatch')
    meta=player.get('microformat',{}).get('playerMicroformatRenderer',{})
    if not details.get('title'):raise ValueError('invalid_video_metadata')
    text='\n\n'.join(filter(None,(details['title'],details.get('shortDescription'))))
    result=post(source,'youtube:'+video,url,text,meta.get('publishDate'))
    from post_metrics import youtube_metrics
    from engagement_metrics import retain_counters
    retain_counters(result,youtube_metrics(body,video))
    result.update(capture_mode='youtube_public_watch',text_may_be_truncated=False)
    return result


def uploads(channel, start):
    if not re.fullmatch(CHANNEL,channel) or type(start) is not int or start<1:raise ValueError('invalid_upload_cursor')
    playlist='UU'+channel[2:]
    from cloud_social import worker_env
    cmd=[sys.executable,'-m','yt_dlp','--ignore-config','--no-plugin-dirs','--no-cache-dir','--skip-download',
         '--flat-playlist','--dump-single-json','--playlist-start',str(start),'--playlist-end',str(start+PAGE-1),
         '--socket-timeout','10','--retries','0','--extractor-retries','0','--','https://www.youtube.com/playlist?list='+playlist]
    result=subprocess.run(cmd,capture_output=True,timeout=35,env=worker_env())
    if result.returncode:
        from media_metadata import failure
        raise ValueError(failure(result.stderr.decode(errors='replace')))
    listing=json.loads(result.stdout)
    if listing.get('id')!=playlist or listing.get('channel_id')!=channel:raise ValueError('playlist_owner_mismatch')
    entries=listing.get('entries',[])
    if not isinstance(entries,list) or len(entries)>PAGE:raise ValueError('invalid_upload_page')
    from candidate_audit import reject
    slots = []
    for entry in entries:
        video = entry.get('id') if isinstance(entry,dict) else None
        if not isinstance(video,str) or not VIDEO.fullmatch(video):
            reject('invalid_video_id')
            slots.append(None)
        elif entry.get('channel_id') not in (None,'',channel):
            reject('playlist_entry_owner_mismatch','youtube:'+video)
            slots.append(None)
        else:
            # A missing flat-playlist owner is resolved against the watch/oEmbed
            # record before publication. Keep physical slots for pagination.
            slots.append(video)
    return slots


def collect(source, previous, *, backfill=False,details=4,seconds=45,reuse_channel=False):
    deadline=time.monotonic()+seconds
    resolved=previous.get('channel_resolved_at')
    try:age=(datetime.now(timezone.utc)-instant(resolved)).total_seconds() if resolved else -1
    except (ValueError,TypeError):age=-1
    cached=(reuse_channel and not backfill and previous.get('url')==source['url'] and
            re.fullmatch(CHANNEL,str(previous.get('channel',''))) and 0<=age<900)
    channel=previous['channel'] if cached else resolve(source,fetch)
    resolved=resolved if cached else now()
    if source.get('backfill_url'):
        u=urlsplit(source['backfill_url'])
        ids=parse_qs(u.query).get('v',[])
        if u.hostname not in ('www.youtube.com','youtube.com') or u.path!='/watch' or len(ids)!=1:raise ValueError('invalid_video_url')
        item=watch(source,channel,ids[0])
        event=source.get('watch_observation')
        if event and not item.get('published_at'):
            # Only repository-authorized dispatches may carry an observation.
            # The hosted reader independently verifies this public card's owner.
            if (not isinstance(event,dict) or event.get('format')!='youtube-watch-microformat-v1'
                    or event.get('url')!=item['post_url'] or event.get('video_id')!=ids[0]
                    or event.get('channel_id')!=channel or not re.fullmatch('[0-9a-f]{64}',event.get('response_sha256',''))):
                raise ValueError('invalid_public_observation')
            published=date(event.get('publishDate'));observed=date(event.get('observed_at'))
            if not published or not observed or not instant(published)<=instant(observed)<=datetime.now(timezone.utc):
                raise ValueError('invalid_public_observation_time')
            item['published_at']=published
            item['public_date_evidence']={k:event[k] for k in ('format','url','video_id','channel_id','publishDate','observed_at','response_sha256')}
        return [item],'ok',[{'tool':'youtube_public_watch','status':'ok','posts':1}],None
    atom_status='ok'
    try:
        body,_=fetch('https://www.youtube.com/feeds/videos.xml?channel_id='+channel)
        rows=parse(source,body,channel,100)
    except Exception as exc:
        if error_code(exc)!='http_404':raise
        # A missing Atom feed is not evidence that the channel has no public uploads.
        rows=[]
        atom_status='http_404'
    head=[p['canonical_key'].split(':',1)[1] for p in rows]
    attempts=[{'tool':'youtube_native_atom','posts':len(rows),'status':atom_status if rows or atom_status!='ok' else 'no_public_posts'}]
    if previous.get('channel')!=channel:previous={}
    state={**previous,'channel':channel,'channel_resolved_at':resolved,'url':source['url'],'head':head,'updated_at':now()}
    cutoff=datetime.min.replace(tzinfo=timezone.utc) if backfill else datetime.now(timezone.utc)-timedelta(hours=48)
    if backfill and not previous.get('history_initialized'):
        state.update(start=1,pending=[],boundary=[],page_recent=False,history_initialized=True)
    elif atom_status=='http_404' and not state.get('start') and not backfill:
        state.update(start=1,next_start=None,pending=[],boundary=[],page_recent=False)
    elif not backfill and head and len(head)>=15 and not set(head).intersection(previous.get('head',[])) and any(
            not p.get('published_at') or instant(p['published_at'])>=cutoff for p in rows):
        # Overlap on every new head gap, retaining the older boundary until covered.
        state.update(start=1,pending=[],boundary=previous.get('boundary',previous.get('head',[])),page_recent=False)
    if not state.get('start'):
        return rows,attempts[0]['status'],attempts,state
    found=[]
    unavailable=0
    try:
        live_uploads = atom_status=='http_404' and not backfill
        first_page = None
        # Draining an acknowledged pending page must make progress even when
        # resolving the upload head consumes the complete live time budget.
        # Recheck the public Atom head above every poll; refresh the missing-Atom
        # playlist head at most once per five minutes while pending details drain.
        last_head = previous.get('uploads_head_checked_at')
        try:head_recent = last_head and 0 <= (datetime.now(timezone.utc)-instant(last_head)).total_seconds() < 300
        except (ValueError,TypeError):head_recent = False
        drain_turn = bool(state.get('pending') and previous.get('drain_pending_next'))
        state['drain_pending_next'] = False
        if live_uploads and not (state.get('pending') and (head_recent or drain_turn)):
            first_page=uploads(channel,1)
            state['uploads_head_checked_at']=now()
            # Guarantee a details-only turn even if the scheduler returns after
            # the head freshness window (or after a worker restart).
            state['drain_pending_next']=True
            known=set(previous.get('uploads_head',[]))
            additions=[video for video in first_page if video and video not in known]
            pending=state.get('pending',[])
            state['uploads_head']=[video for video in first_page if video]
            if first_page and not known.intersection(first_page):
                # First use, or a whole page arrived since the previous poll.
                state.update(start=1,next_start=PAGE-4 if len(first_page)==PAGE else None)
            if additions:
                state['pending']=list(dict.fromkeys(additions+pending))
            if not state.get('pending') and state.get('start')==1 and not additions:
                state['start']=state.get('next_start')
        if not state.get('pending') and state.get('start'):
            start=state['start']
            ids=first_page if start==1 and first_page is not None else uploads(channel,start)
            boundary=set(state.get('boundary',[]))
            end=next((i for i,v in enumerate(ids) if v in boundary),len(ids))
            state.update(pending=[v for v in ids[:end] if v and v not in head],page_recent=False,
                         next_start=start+PAGE-5 if len(ids)==PAGE and end==len(ids) else None)
            # Native entries also count toward the age of the page.
            if set(head).intersection(ids[:end]):
                state['page_recent']=any(not p.get('published_at') or instant(p['published_at'])>=cutoff for p in rows)
        for video in state['pending'][:details]:
            if time.monotonic()>=deadline:break
            try:item=watch(source,channel,video)
            except Exception as error:
                reason = error_code(error)
                if reason not in ('video_unavailable','video_owner_mismatch','http_404','http_410'):raise
                from candidate_audit import reject
                reject(reason,'youtube:'+video)
                unavailable+=1
                state['pending']=state['pending'][1:]
                time.sleep(.5)
                continue
            if not item['published_at'] or instant(item['published_at'])>=cutoff:
                found.append(item);state['page_recent']=True
            state['pending']=state['pending'][1:]
            time.sleep(.5)
        if not state['pending']:
            state['start']=state.get('next_start') if state['page_recent'] else None
            if not state['start']:state.pop('boundary',None)
        status='ok'
    except Exception as error:
        status=error_code(error)
    attempts.append({'tool':'youtube_uploads','status':status,'posts':len(found),
                     'unavailable_details':unavailable,'remaining_details':len(state.get('pending',[])),'more_history':bool(state.get('start'))})
    if not rows and not found and status=='ok':
        status='no_new_posts' if not state.get('pending') and not state.get('start') else 'partial_results'
    return rows+found,status,attempts,state
