"""Discover and reread older public Facebook permalinks on hosted runners."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup
from browser_public import context_for, facebook_owned, page_error, normalize_rows
from facebook_feed import CARDS, combine, extract, objects, story_post
from run import HOSTS, error_code
from social_links import identity


def candidate(url):
    found=identity(url)
    if found and found['platform']=='facebook':
        return {**found,'url':found['url'].replace('.php/?','.php?')}
    # Search results can use the long SEO URL with a title before the numeric ID.
    if not isinstance(url,str):
        return None
    u=urlsplit(url)
    match=re.fullmatch(r'/([\w.-]+)/posts/[^/]+/(\d+)/?',u.path)
    if match and u.scheme=='https' and u.hostname in HOSTS['facebook'] and not u.username and not u.password and not any(k in parse_qs(u.query) for k in ('comment_id','reply_comment_id')):
        return identity('https://www.facebook.com/'+match[1]+'/posts/'+match[2])
    return None


def source_tab(url,tab):
    u=urlsplit(url)
    if u.path.strip('/')=='profile.php':
        owner=parse_qs(u.query).get('id',[''])[0]
        if owner.isdigit():
            return 'https://www.facebook.com/profile.php?'+urlencode({'id':owner,'sk':tab})
    if re.fullmatch(r'/(?:groups/)?[\w.-]+/?',u.path):
        return 'https://www.facebook.com'+u.path.rstrip('/')+'/'+tab+'/'
    return None


def indexed(source):
    import requests
    u=urlsplit(source['url'])
    owner=parse_qs(u.query).get('id',[u.path.strip('/')])[0]
    if not re.fullmatch(r'[\w.-]+',owner):
        return [],'unsupported_search_shape'
    query='site:facebook.com/'+owner+'/posts/' if not owner.isdigit() else 'site:facebook.com/permalink.php "'+owner+'"'
    with requests.Session() as session:
        session.trust_env=False
        with session.get('https://www.bing.com/search',params={'q':query,'format':'rss'},timeout=(4,7),stream=True,allow_redirects=False) as reply:
            if reply.status_code!=200:
                return [],'http_'+str(reply.status_code)
            raw=reply.raw.read(1024*1024+1,decode_content=True)
    if len(raw)>1024*1024 or b'<rss' not in raw[:500]:
        return [],'index_unavailable'
    import xml.etree.ElementTree as ET
    urls=[f['url'] for link in ET.fromstring(raw).findall('./channel/item/link')
          if (f:=candidate(link.text)) and facebook_owned(source,{},f['url'])]
    return urls,'ok' if urls else 'no_matching_links'


def media_stories(payload,media_id):
    stack=[payload]
    while stack:
        obj=stack.pop()
        if isinstance(obj,list):
            stack.extend(obj)
        elif isinstance(obj,dict):
            if str(obj.get('id',''))==media_id and obj.get('__typename') in ('Photo','Video'):
                parent=obj.get('creation_story')
                if isinstance(parent,dict):
                    yield parent
            stack.extend(v for k,v in obj.items() if k not in ('comments','feedback') and isinstance(v,(list,dict)))


async def page_data(page,url,detail=False):
    response=await page.goto(url,wait_until='domcontentloaded',timeout=12000)
    if response and response.status>=400:
        raise ValueError('http_'+str(response.status))
    if urlsplit(page.url).hostname not in HOSTS['facebook']:
        raise ValueError('unexpected_destination')
    if page_error(page.url,''):
        raise ValueError('login_required')
    await page.wait_for_timeout(650)
    failure=page_error(page.url,await page.locator('body').inner_text(timeout=2500))
    if failure:
        raise ValueError(failure)
    scripts=await page.evaluate("""() => Array.from(document.querySelectorAll('script[type="application/json"]'))
      .map(n=>n.textContent).filter(s=>s.length<=8388608 && /timeline_list_feed_units|group_feed|node_v2|creation_story/.test(s))""")
    nodes=[];parents=[]
    media_key=candidate(url)
    is_media=detail and media_key and re.search(r'/photo[/.?]|/videos/|/watch[/?]',media_key['url'])
    for raw in scripts:
        for doc in objects(raw):
            stories,_=extract(doc,detail=detail)
            nodes.extend(stories)
            if is_media:
                parents.extend(media_stories(doc,media_key['canonical_key'].split(':',1)[1]))
    links=await page.locator('a[href]').evaluate_all('(a)=>a.slice(0,1600).map(n=>n.href)')
    return nodes,links,parents


def strictly_older(row,anchor):
    if not anchor or not row.get('published_at') or row['canonical_key']==anchor['key']:
        return False
    if anchor.get('story_id') and anchor['story_id']==row.get('facebook_story_id'):
        return False
    return datetime.fromisoformat(row['published_at']) < datetime.fromisoformat(anchor['date'])


async def collect_source(browser,source,plan,search_state):
    record={'id':source['id'],'url':source['url'],'name':source['name'],'stages':{},'reads':[],
            'anchor':plan['anchors'].get(source['id']),'accepted':[]}
    anchor=record['anchor']
    pool={}
    def offer(url,via):
        f=candidate(url)
        if f and (not anchor or f['canonical_key']!=anchor['key']):
            pool.setdefault(f['canonical_key'],{'url':f['url'],'via':via})

    accepted={}
    known=plan['known']
    def accept(row,via):
        if row and row.get('published_at') and (not anchor or strictly_older(row,anchor)) and (row['text'].strip() or row['media_urls']):
            row.update(facebook_older_method=via)
            combine(accepted,[row])

    ctx=await context_for(browser)
    end=time.monotonic()+90
    try:
        page=await ctx.new_page()
        if not anchor:
            try:
                nodes,_,_=await page_data(page,source['url'])
                head=[r for n in nodes if (r:=story_post(source,n)) and r.get('published_at')]
                if head:
                    row=max(head,key=lambda r:r['published_at'])
                    anchor=record['anchor']={'key':row['canonical_key'],'date':row['published_at'],'story_id':row['facebook_story_id'],'basis':'live_head'}
                record['stages']['head']='ok' if anchor else 'no_verified_dated_head'
            except Exception as e:
                record['stages']['head']=error_code(e)
            if record['stages']['head'] in ('http_429','challenge_required'):
                return record,[]
        for url in plan.get('citations',{}).get(source['id'],[]):offer(url,'publisher_citation')
        for tab in ('posts','photos'):
            target=source_tab(source['url'],tab)
            if not target:
                continue
            try:
                nodes,links,_=await page_data(page,target)
                for n in nodes:
                    row=story_post(source,n)
                    accept(row,tab+'_tab')
                count=0
                for link in links:
                    f=candidate(link)
                    if f and facebook_owned(source,{},link):
                        offer(link,tab+'_tab');count+=1
                    elif f and tab=='photos' and f['url'].startswith('https://www.facebook.com/photo/'):
                        offer(link,'photo_tab_link');count+=1
                record['stages'][tab]='public_links_'+str(count)
            except Exception as e:
                record['stages'][tab]=error_code(e)
                if error_code(e) in ('http_429','challenge_required','http_403','http_401'):
                    break
        if not search_state['blocked']:
            try:
                urls,status=await asyncio.to_thread(indexed,source)
                for url in urls:offer(url,'public_search')
                record['stages']['search']=status
                if status in ('http_429','http_403','index_unavailable'):search_state['blocked']=True
            except Exception as e:
                record['stages']['search']=error_code(e)
        else:
            record['stages']['search']='index_deferred'
        for url in plan['crosslinks'].get(source['id'],[]):offer(url,'public_crosslink')
        for url in plan['seeds'].get(source['id'],[]):offer(url,'archive_permalink')
        record['candidates']=len(pool)
        if any(v in ('http_429','challenge_required') for k,v in record['stages'].items() if k!='search'):
            record['stages']['detail']='rate_or_challenge_stop'
        else:
            queue=list(pool.items())
            for index,(key,entry) in enumerate(queue):
                if index>=20:
                    record['stages']['detail']='candidate_budget';break
                if time.monotonic()>end:
                    record['stages']['detail']='time_budget';break
                try:
                    nodes,_,parents=await page_data(page,entry['url'],True)
                    for parent in parents:
                        row=story_post(source,parent)
                        if row:
                            accept(row,entry['via'])
                        f=candidate(parent.get('url') or parent.get('wwwURL'))
                        if f and facebook_owned(source,{},f['url']) and (not anchor or f['canonical_key']!=anchor['key']) and f['canonical_key'] not in pool:
                            offer(f['url'],entry['via'])
                            queue.append((f['canonical_key'],pool[f['canonical_key']]))
                    # Rebind owner from the live post. A known URL alone does not establish authorship.
                    rows=[]
                    for n in nodes:
                        row=story_post(source,n)
                        if row and (row['canonical_key']==key or 'facebook:'+row['facebook_story_id']==key):
                            rows.append(row)
                    cards=normalize_rows(source,await page.evaluate(CARDS),100)
                    resolved={}
                    combine(resolved,rows)
                    combine(resolved,[r for r in cards if r['canonical_key']==key])
                    for row in resolved.values():accept(row,entry['via'])
                    status='older_verified' if any(strictly_older(r,anchor) and (r['text'] or r['media_urls']) for r in resolved.values()) else 'not_older_or_unverified'
                except Exception as e:
                    status=error_code(e)
                record['reads'].append({'candidate':key,'via':entry['via'],'status':status})
                if status in ('http_429','challenge_required'):break
    finally:
        await ctx.close()
    # Alias URLs for the same native Facebook story are one post.
    distinct={}
    for row in accepted.values():
        native=row.get('facebook_story_id') or row['canonical_key']
        old=distinct.get(native)
        if old is None or (old.get('text_may_be_truncated') and not row.get('text_may_be_truncated')):
            distinct[native]=row
    if not anchor and distinct:
        newest=max(distinct.values(),key=lambda r:datetime.fromisoformat(r['published_at']))
        anchor=record['anchor']={'key':newest['canonical_key'],'date':newest['published_at'],
                                 'story_id':newest.get('facebook_story_id'),'basis':'newest_verified_candidate'}
        distinct={k:v for k,v in distinct.items() if strictly_older(v,anchor)}
    for row in distinct.values():
        row.update(facebook_before_key=anchor['key'],facebook_before_date=anchor['date'])
        row['facebook_known_before']=row['canonical_key'] in known or row.get('facebook_story_id') in plan['known_native']
    record['accepted']=[r['canonical_key'] for r in distinct.values()]
    return record,list(distinct.values())


async def run(part,parts):
    from cloud_social import require_cloud
    from playwright.async_api import async_playwright
    require_cloud()
    plan=json.loads(Path('input/plan.json').read_text())
    results=[];posts={};state={'blocked':False}
    async with async_playwright() as pw:
        browser=await pw.chromium.launch()
        if plan['phase']=='resume':
            from facebook_continue import resume
            if (part,parts)!=(0,1):raise ValueError('resume_requires_one_shared_reader')
            results,items,requests=await resume(browser,plan)
            posts={r['canonical_key']:r for r in items}
            print(json.dumps({'unique_permalink_checks':requests,'sources':len(results)}))
        else:
            for source in plan['sources'][part::parts]:
                record,items=await collect_source(browser,source,plan,state)
                results.append(record)
                for row in items:posts[row['canonical_key']]=row
                print(json.dumps({'source_id':source['id'],'older':len(items),'candidates':record.get('candidates',0)}),flush=True)
                await asyncio.sleep(1)
        await browser.close()
    Path('out').mkdir(exist_ok=True)
    Path('out/part.json').write_text(json.dumps({'phase':plan['phase'],'baseline':plan['baseline'],'sources':results,'posts':list(posts.values())},ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--part',type=int,required=True);p.add_argument('--parts',type=int,required=True);a=p.parse_args()
    asyncio.run(run(a.part,a.parts))
