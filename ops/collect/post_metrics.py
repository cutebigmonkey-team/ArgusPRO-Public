"""Bounded public counter refreshes for already verified archive posts.

No login, cookies, playback, or media downloads. A missing value stays unknown.
"""
import json
import re
import time
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from engagement_metrics import counters, displayed_count, retain_counters, telegram_counters


def tiktok_metrics(body, item):
    target = re.fullmatch(r'/@([\w.-]+)/video/(\d+)', urlsplit(item['post_url']).path)
    if not target: return {}
    soup = BeautifulSoup(body, 'html.parser')
    candidates = []
    for script in soup.select('script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, script#__FRONTITY_CONNECT_STATE__'):
        try: data = json.loads(script.string or script.get_text())
        except ValueError: continue
        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, list): stack.extend(value)
            elif isinstance(value, dict):
                author = value.get('author')
                author = author.get('uniqueId') if isinstance(author, dict) else author
                if str(value.get('id')) == target[2] and str(author or '').lower() == target[1].lower():
                    candidates.append(value)
                else: stack.extend(v for v in value.values() if isinstance(v, (dict,list)))
    result = {}
    for row in candidates: retain_counters(result,row)
    return result.get('engagement',{})


def youtube_metrics(body, video):
    def js(name):
        for match in re.finditer(rb'\b'+name.encode()+rb'\s*=\s*',body):
            tail=body[match.end():].decode('utf-8')
            try:
                if tail.startswith("'"):
                    # Mobile YouTube serializes JSON inside a JS string. Parse
                    # the string as data; never execute scripts from the page.
                    from yt_dlp.utils import js_to_json
                    literal=re.match(r"'(?:[^'\\]|\\.)*'",tail)
                    value=json.loads(js_to_json(literal[0])) if literal else None
                else:value=json.JSONDecoder().raw_decode(tail)[0]
                if isinstance(value,str):value=json.loads(value)
                if isinstance(value,dict):return value
            except ValueError:continue
        return {}
    player = js('ytInitialPlayerResponse')
    details = player.get('videoDetails',{})
    data=js('ytInitialData')
    endpoint=data.get('currentVideoEndpoint',{}).get('watchEndpoint',{}).get('videoId')
    if details.get('videoId') not in (None,video) or endpoint not in (None,video):return {}
    if details.get('videoId')!=video and endpoint!=video:return {}
    result = {}
    views = str(details.get('viewCount',''))
    if re.fullmatch(r'\d{1,16}',views) and int(views)<=9007199254740991: result['views']=int(views)
    def text(value):
        if isinstance(value,str):return value
        if not isinstance(value,dict):return ''
        return value.get('simpleText') or ''.join(r.get('text','') for r in value.get('runs',[]))
    def count(value):
        label=text(value).strip()
        match=re.search(r'\d[\d,\s.]*[KMB]?',label,re.I)
        return displayed_count(match[0]) if match else None
    layout=data.get('contents',{})
    layout=layout.get('twoColumnWatchNextResults') or layout.get('singleColumnWatchNextResults') or {}
    content=layout.get('results',{}).get('results',{}).get('contents',[])
    content=content+[section for item in content for section in item.get('slimVideoMetadataSectionRenderer',{}).get('contents',[])]
    for section in content:
        primary=section.get('videoPrimaryInfoRenderer',{})
        stack=list(primary.get('videoActions',{}).get('menuRenderer',{}).get('topLevelButtons',[]))
        stack.extend(section.get('slimVideoActionBarRenderer',{}).get('buttons',[]))
        while stack:
            value=stack.pop()
            if isinstance(value,list):stack.extend(value)
            elif isinstance(value,dict):
                if value.get('iconName')=='LIKE':
                    amount=displayed_count(text(value.get('title')))
                    if amount is not None:result['likes']=amount
                for field in ('label','accessibilityText'):
                    label=value.get(field)
                    if isinstance(label,str) and (re.search(r'^\d[\d,\s.]*[KMB]?\s+likes?\b',label,re.I) or
                            re.search(r'^like this video along with [\d,]+ other people',label,re.I)):
                        amount=count(label)
                        if amount is not None:result['likes']=amount
                stack.extend(v for k,v in value.items() if isinstance(v,(dict,list)) and not k.lower().startswith('dislike'))
        for entry in section.get('itemSectionRenderer',{}).get('contents',[]):
            amount=count(entry.get('commentsEntryPointHeaderRenderer',{}).get('commentCount'))
            if amount is not None:result['comments']=amount
    for panel in data.get('engagementPanels',[]):
        panel=panel.get('engagementPanelSectionListRenderer',{})
        if panel.get('panelIdentifier') not in ('comment-item-section','engagement-panel-comments-section'):continue
        amount=count(panel.get('header',{}).get('engagementPanelTitleHeaderRenderer',{}).get('contextualInfo'))
        if amount is not None:result['comments']=amount
    return result


async def bounded_refresh(source,items,previous,seconds=6):
    """A late optional counter response must not cancel or mutate collected news."""
    import asyncio,copy
    updated=copy.deepcopy(items)
    try:
        result=await asyncio.wait_for(asyncio.to_thread(refresh,source,updated,previous),seconds)
    except TimeoutError:
        retained={k:previous[k] for k in ('metrics_checked_at','metrics_offset') if k in previous}
        return items,({'tool':'public_post_metrics','posts':0,'status':'deferred_budget'},retained)
    return (updated if result else items),result


def refresh(source,items,previous,reader=None,clock=time.time):
    """One rotating post per source per 15 minutes; retain normal polling budgets."""
    from run import fetch,error_code
    from browser_public import page_error
    from source_health import instant
    reader=reader or fetch
    platform=source['platform']
    if platform not in ('telegram','youtube','tiktok') or not items:return None
    now=clock()
    last=previous.get('metrics_checked_at')
    if last and now-instant(last).timestamp()<900:return None
    offset=int(previous.get('metrics_offset',0))%len(items)
    item=items[offset]
    attempt={'tool':'public_post_metrics','posts':0,'status':'not_exposed'}
    try:
        url=item['post_url']
        target=url+'?embed=1&mode=tme' if platform=='telegram' else url
        body,final=reader(target,timeout=8)
        if urlsplit(final).hostname!=urlsplit(target).hostname:raise ValueError('unexpected_destination')
        if error:=page_error(final,body.decode('utf-8','replace')):raise ValueError(error)
        values={}
        if platform=='telegram':
            identity=item['canonical_key'].removeprefix('telegram:')
            for node in BeautifulSoup(body,'html.parser').select('.tgme_widget_message[data-post]'):
                if node['data-post'].lower()==identity:values=telegram_counters(node);break
        elif platform=='tiktok':values=tiktok_metrics(body,item)
        else:values=youtube_metrics(body,item['canonical_key'].removeprefix('youtube:'))
        if values:
            retain_counters(item,values)
            attempt.update(status='ok',posts=1,known=list(values))
    except Exception as error:attempt['status']=error_code(error)
    from datetime import datetime,timezone
    return attempt,{'metrics_checked_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),'metrics_offset':offset+1}
