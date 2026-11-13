"""Offline discovery of post permalinks; discovery never implies captured text."""
import html
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit


def identity(value):
    if not isinstance(value,str) or len(value)>3000: return None
    try:
        u=urlsplit(html.unescape(value.strip()))
        if u.scheme not in ('http','https') or u.username or u.password or u.port not in (None,80,443): return None
        host=(u.hostname or '').lower(); path=u.path.rstrip('/'); query=parse_qs(u.query)
        if any(k in query for k in ('comment_id','reply_comment_id')): return None
        if host in ('instagram.com','www.instagram.com'):
            match=re.fullmatch(r'/(?:[A-Za-z0-9_.]{1,30}/)?(p|reel|reels)/([A-Za-z0-9_-]{5,64})',path)
            if not match: return None
            kind='p' if match[1]=='p' else 'reel'; key=match[2]
            return {'canonical_key':'instagram:'+key,'platform':'instagram','url':f'https://www.instagram.com/{kind}/{key}/'}
        if host not in ('facebook.com','www.facebook.com','m.facebook.com','mbasic.facebook.com'): return None
        key=None; canonical=None
        if path in ('/photo.php','/photo','/permalink.php','/story.php','/watch'):
            field='fbid' if path in ('/photo.php','/photo') else 'v' if path=='/watch' else 'story_fbid'
            key=query.get(field,[''])[0]
            values={field:key}
            if field=='story_fbid':
                page=query.get('id',[''])[0]
                if not page.isdigit(): return None
                values['id']=page
            canonical='https://www.facebook.com'+path+('?' if path.endswith('.php') else '/?')+urlencode(values)
        else:
            match=re.fullmatch(r'/((?:(?:groups/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+)/)?(?:posts|videos|permalink|reel|reels))/([A-Za-z0-9_-]+)',path)
            if match:
                key=match[2]; canonical='https://www.facebook.com'+path+'/'
            else:
                match=re.fullmatch(r'/[A-Za-z0-9_.-]+/photos/(?:[^/]+/)?(\d+)',path)
                if match: key=match[1]; canonical='https://www.facebook.com/photo/?fbid='+key
        if not key or not re.fullmatch(r'(?:\d+|pfbid[A-Za-z0-9]+)',key): return None
        return {'canonical_key':'facebook:'+key,'platform':'facebook','url':canonical}
    except (ValueError,TypeError): return None


def public_urls(value,base=''):
    from bs4 import BeautifulSoup
    if not isinstance(value,str): return []
    text=value[:1000000]; urls=[]
    if '<' in text:
        soup=BeautifulSoup(text,'html.parser')
        for tag in soup(['script','style','template','noscript']): tag.decompose()
        urls=[urljoin(base,a.get('href','')) for a in soup.select('a[href]')[:2000]]
        text=soup.get_text(' ',strip=True)
    urls+=re.findall(r'https?://[^\s<>"\u200b]+',text)
    return [html.unescape(url).rstrip('.,;:!?)]}') for url in urls]


def post_links(urls):
    result={}
    for url in urls:
        item=identity(url)
        if item: result[item['canonical_key']]=item['url']
    return list(result.values())[:100]


def extract(value,base=''):
    return post_links(public_urls(value,base))


def profile_links(urls):
    from source_discovery import profile
    if not isinstance(urls,list):
        return []
    return list(dict.fromkeys(target[1] for url in urls if (target:=profile(url))))[:100]


def outbound(value,base=''):
    urls=public_urls(value,base)
    return {'outbound_social_links':post_links(urls),'outbound_profile_links':profile_links(urls)}


def validated_urls(values):
    if not isinstance(values,list): return []
    return list(dict.fromkeys(item['url'] for value in values[:200] if (item:=identity(value))))[:100]


def from_posts(posts,disabled=()):
    records=[]
    for post in posts:
        if post.get('sample') or post.get('platform') not in ('telegram','rss','mailru'): continue
        origin=post.get('post_url') or post.get('url')
        if not isinstance(origin,str): continue
        urls=extract(post.get('text',''),origin)+post.get('outbound_social_links',[])
        for url in dict.fromkeys(urls):
            item=identity(url)
            if not item or item['platform'] in disabled: continue
            records.append({**item,'status':'link_only','content_status':'not_fetched','text':'',
                'published_at':None,'observed_at':post['observed_at'],
                'evidence':[{'method':'cross_link','source_url':origin,'source_platform':post['platform'],
                             'source_id':post.get('canonical_key') or post.get('id','')}]})
    return records


def merge_records(previous,incoming):
    result={p['canonical_key']:dict(p) for p in previous}
    for row in incoming:
        key=row['canonical_key']; old=result.get(key)
        if old is None:
            result[key]={**row,'first_seen_at':row['observed_at']}; continue
        evidence={str(sorted(e.items())):e for e in old['evidence']+row['evidence']}
        result[key]={**old,'observed_at':max(old['observed_at'],row['observed_at']),
                     'evidence':list(evidence.values())[:20]}
        if row['status']=='search_snippet':
            result[key].update(status='search_snippet',snippet=row.get('snippet',''),title=row.get('title',''))
    if len(result)>20000: raise ValueError('discovery_index_needs_sharding')
    return sorted(result.values(),key=lambda r:r['canonical_key'])
