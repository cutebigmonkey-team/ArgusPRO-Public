"""Isolated anonymous Instaloader / facebook-scraper worker. No media downloads."""
import datetime as dt
import itertools
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, parse_qs


def classify(error):
    message=(type(error).__name__+' '+str(error)).lower()
    for pattern,status in ((r'429|too many requests','http_429'),(r'403|forbidden','http_403'),
        (r'login|log in|logged in|privateprofile|private profile','login_required'),
        (r'captcha|checkpoint','challenge_required'),(r'notexists|not found|404','unavailable'),
        (r'timeout|timed out','timeout')):
        if re.search(pattern,message):return status
    return 'extraction_failed'


class PartialCollection(Exception):
    """Already extracted rows plus a fixed safe reason; no upstream message."""
    def __init__(self,rows,error):
        super().__init__('partial_results')
        self.rows=rows
        self.reason=classify(error)


def instagram(url, limit):
    import instaloader
    loader=instaloader.Instaloader(download_pictures=False,download_videos=False,
        download_video_thumbnails=False,download_geotags=False,download_comments=False,
        save_metadata=False,post_metadata_txt_pattern='',max_connection_attempts=1,
        request_timeout=12,fatal_status_codes=[401,403,429],quiet=True)
    rows=[]
    try:
        path=urlsplit(url).path
        match=re.fullmatch(r'/(?:p|reel)/([A-Za-z0-9_-]+)/?',path)
        if match:
            posts=[instaloader.Post.from_shortcode(loader.context,match[1])]
        else:
            profile=instaloader.Profile.from_username(loader.context,path.strip('/'))
            if profile.is_private:raise ValueError('private profile')
            posts=profile.get_posts()
        for p in itertools.islice(posts,limit):
            rows.append({'url':'https://www.instagram.com/p/'+p.shortcode+'/',
                         'owner':p.owner_username,'text':p.caption or '',
                         'published_at':p.date_utc.replace(tzinfo=dt.timezone.utc).isoformat(),
                         'media_urls':[p.url]})
    except Exception as error:
        if rows:raise PartialCollection(rows,error) from None
        raise
    finally:loader.close()
    return rows


def facebook(url, limit):
    import facebook_scraper
    parsed=urlsplit(url);path=parsed.path.strip('/')
    if re.search(r'/(posts|videos|reel|permalink)/',parsed.path) or 'story_fbid' in parse_qs(parsed.query):
        target={'post_urls':[url]}
    elif path=='profile.php':target={'account':parse_qs(parsed.query)['id'][0]}
    elif path.startswith('groups/'):target={'group':path.split('/')[1]}
    else:target={'account':path}
    rows=[]
    try:
        for p in itertools.islice(facebook_scraper.get_posts(**target,pages=1,timeout=12,
            options={'comments':False,'reactors':False,'reactions':False,'allow_extra_requests':False}),limit):
            timestamp=p.get('time')
            # The library's naive timestamp is not assigned a guessed timezone.
            stamp=timestamp.isoformat() if isinstance(timestamp,dt.datetime) and timestamp.tzinfo else None
            rows.append({'url':p.get('post_url',''),'text':p.get('text',''),
                         'owner':p.get('username'),'owner_id':p.get('user_id'),
                         'published_at':stamp,'media_urls':p.get('images') or []})
    except Exception as error:
        if rows:raise PartialCollection(rows,error) from None
        raise
    return rows


def main():
    from runtime_policy import require_cloud
    require_cloud()
    tool,url,count,output=sys.argv[1:]
    limit=int(count)
    if not 1<=limit<=100:raise ValueError('invalid_limit')
    host=urlsplit(url).hostname
    if tool=='instaloader' and host not in ('instagram.com','www.instagram.com'):raise ValueError('invalid_source')
    if tool=='facebook-scraper' and host not in ('facebook.com','www.facebook.com','m.facebook.com','mbasic.facebook.com'):raise ValueError('invalid_source')
    try:
        if tool=='instaloader':rows=instagram(url,limit)
        elif tool=='facebook-scraper':rows=facebook(url,limit)
        else:raise ValueError('invalid_tool')
        result={'status':'ok' if rows else 'no_public_posts','rows':rows}
    except PartialCollection as error:
        restricted={'http_401','http_403','http_429','login_required','challenge_required'}
        result={'status':error.reason if error.reason in restricted else 'partial_results',
                'rows':error.rows,'stop_reason':error.reason,'fetch_complete':False}
    except Exception as error:result={'status':classify(error),'rows':[]}
    Path(output).write_text(json.dumps(result),encoding='utf-8')


if __name__=='__main__':main()
