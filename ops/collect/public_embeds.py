"""Read the platforms' own public embeds. No accounts, tokens or proxy services."""
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from candidate_audit import count


def timestamp(value):
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            value=float(value)
            if 1000000000000 <= value < 4102444800000:
                value /= 1000
            return dt.datetime.fromtimestamp(value,dt.timezone.utc).isoformat() if 0<value<4102444800 else None
        except (ValueError, OverflowError, OSError):return None
    if isinstance(value,str):
        try:return parsedate_to_datetime(value).isoformat()
        except (ValueError,TypeError):return value
    return None


def handle(source):
    path=urlsplit(source['url']).path.strip('/').split('/')
    name=path[0].lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',name):raise ValueError('invalid_profile')
    return name


def publisher_tiktok_rows(source, body, page_url):
    """Read the publisher's own native TikTok blockquotes without loading TikTok."""
    from candidate_feeds import tiktok_target
    from run import post
    owner=handle(source).lower()
    rows={}
    for block in BeautifulSoup(body,'html.parser').select('blockquote.tiktok-embed[cite][data-video-id]'):
        target=tiktok_target(block['cite'])
        if not target or target[1].lower()!=owner or target[2]!=block['data-video-id']:
            continue
        section=block.find('section',recursive=False)
        author=section.find('a',href=True) if section else None
        byline=tiktok_target(author['href']) if author else None
        if not byline or byline[2] or byline[1].lower()!=owner:
            continue
        author.decompose()
        for node in section.select('script,style,a[href]'):
            link=urlsplit(node.get('href',''))
            if node.name!='a' or (link.hostname in ('www.tiktok.com','tiktok.com') and link.path.startswith('/music/')):
                node.decompose()
        stamp=section.select_one('time[datetime]')
        text=section.get_text(' ',strip=True)
        if text.lower()=='@'+owner:text=''
        media=[n['src'] for n in section.select('video[src],video source[src],img[src]')]
        if not text and not media:continue
        key='tiktok:'+target[2]
        item=post(source,key,'https://www.tiktok.com/@'+owner+'/video/'+target[2],text,
                  stamp['datetime'] if stamp else None,media)
        if not item['text'] and not item['media_urls']:continue
        item.update(capture_mode='publisher_tiktok_embed',publisher_embed_url=page_url,text_may_be_truncated=True)
        rows[key]=item
    return list(rows.values())


def publisher_tiktok(source, limit):
    from crawl_common import CrawlState
    from import_sheet import is_verified_profile
    from run import error_code
    from url_identity import same_site
    refs=source.get('references',[])
    homes=[r['url'] for r in refs if r.get('kind')=='verified_public_profile']
    pages=list(dict.fromkeys(r['url'] for r in refs if r.get('kind')=='publisher_embed'
                             and any(same_site(r['url'],home) for home in homes)))[:4]
    rows={}; errors=[]
    if source['platform']!='tiktok' or not is_verified_profile(source) or not pages:
        return [],{'tool':'publisher_tiktok_embed','status':'no_verified_pages','posts':0}
    reader=CrawlState({'platform':'rss','url':pages[0]},min(5,limit),len(pages))
    for url in pages:
        if not same_site(url,pages[0]):continue
        try:
            body,final=reader.read(url)
            for item in publisher_tiktok_rows(source,body,final):
                rows[item['canonical_key']]=item
        except Exception as error:
            errors.append(error_code(error))
            if reader.stopped:break
        if len(rows)>=limit:break
    items=list(rows.values())[:limit]
    return items,{'tool':'publisher_tiktok_embed','status':'ok' if items else errors[-1] if errors else 'unverified_markup',
                  'posts':len(items),'errors':errors,'keys':[r['canonical_key'] for r in items]}


def parse_tiktok(body,source,limit):
    from run import post
    name=handle(source);soup=BeautifulSoup(body,'html.parser');items={}
    script=soup.find('script',id='__FRONTITY_CONNECT_STATE__')
    if not script:return []
    try:data=json.loads(script.string or script.get_text())
    except ValueError:return []
    # Scope to this exact profile, not recommended videos elsewhere in the payload.
    profile=data.get('source',{}).get('data',{}).get('/embed/@'+name,{})
    videos=profile.get('videoList') or []
    count('raw_input',len(videos))
    for row in videos:
        if not isinstance(row,dict):continue
        identity=str(row.get('id',''))
        if not re.fullmatch(r'\d{8,}',identity):continue
        author=row.get('author')
        author=author.get('uniqueId') if isinstance(author,dict) else author
        if author and str(author).lower()!=name.lower():
            count('source_owner_mismatch')
            continue
        cover=row.get('cover') or row.get('coverUrl') or ''
        if isinstance(cover,dict):cover=cover.get('url','')
        item=post(source,'tiktok:'+identity,'https://www.tiktok.com/@'+name+'/video/'+identity,
                  str(row.get('desc') or row.get('title') or ''),timestamp(row.get('createTime') or row.get('create_time')),[cover])
        item.update(capture_mode='public_creator_embed',text_may_be_truncated=True)
        from engagement_metrics import retain_counters
        retain_counters(item,row)
        items[item['canonical_key']]=item
        if len(items)>=limit:break
    return list(items.values())


def parse_x(body,source,limit):
    from run import post
    name=handle(source);soup=BeautifulSoup(body,'html.parser');script=soup.find('script',id='__NEXT_DATA__')
    if not script:return []
    try:data=json.loads(script.string or script.get_text())
    except ValueError:return []
    entries=data.get('props',{}).get('pageProps',{}).get('timeline',{}).get('entries',[]);items={}
    count('raw_input',len(entries))
    for entry in entries:
        if not isinstance(entry,dict):continue
        row=entry.get('content',{}).get('tweet',{})
        if not isinstance(row,dict):continue
        user=row.get('user') or {};identity=str(row.get('id_str') or '')
        if str(user.get('screen_name','')).lower()!=name.lower():
            count('source_owner_mismatch')
            continue
        if not identity.isdigit():
            count('invalid_post_identity')
            continue
        media=[]
        for part in (row.get('extended_entities') or row.get('entities') or {}).get('media',[]):
            if not isinstance(part,dict):continue
            videos=[v for v in (part.get('video_info') or {}).get('variants',[]) if v.get('content_type')=='video/mp4']
            if videos:
                media.append(max(videos,key=lambda v:v.get('bitrate',0)).get('url'))
            media.append(part.get('media_url_https'))
        item=post(source,'x:'+identity,'https://x.com/'+name+'/status/'+identity,
                  str(row.get('full_text') or row.get('text') or ''),timestamp(row.get('created_at')),media)
        refs=[]
        for label,ref in [('replied_to',row.get('in_reply_to_status_id_str')),('quoted',row.get('quoted_status_id_str')),
                          ('retweeted',(row.get('retweeted_status') or {}).get('id_str'))]:
            if ref and str(ref).isdigit():
                refs.append({'type':label,'id':str(ref)})
        item.update(capture_mode='public_timeline_embed',text_may_be_truncated=not bool(row.get('full_text')),x_references=refs)
        from engagement_metrics import counters
        metrics = counters(row)
        if any(v is not None for v in metrics.values()): item['engagement'] = metrics
        if not item['text'] and not item['media_urls']:
            count('empty_content')
            continue
        if item['canonical_key'] in items:
            count('duplicate_identity')
        items[item['canonical_key']]=item
    # A public embed is already a bounded snapshot and may put pinned posts first.
    return sorted(items.values(), key=lambda r:int(r['canonical_key'][2:]), reverse=True)


def collect(source,limit):
    from run import fetch
    name=handle(source)
    if source['platform']=='tiktok':
        body,_=fetch('https://www.tiktok.com/embed/@'+name)
        return parse_tiktok(body,source,limit)
    if source['platform']=='x':
        from crawl_common import CrawlState
        from candidate_audit import current
        url='https://syndication.twitter.com/srv/timeline-profile/screen-name/'+name
        # CrawlState owns a separate counter for its website parsers.
        counters=current.get()
        reader=CrawlState({'platform':'rss','url':url},1,1)
        current.set(counters)
        body,_=reader.read(url)
        return parse_x(body,source,limit)
    raise ValueError('unsupported_embed')
