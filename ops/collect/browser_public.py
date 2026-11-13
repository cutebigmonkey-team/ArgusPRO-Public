"""Bounded, logged-out browser collection, with incremental DOM extraction."""
import asyncio
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qs, urljoin


def facebook_owned(source,row,url):
    """Reject comments and unrelated/recommended stories in a profile feed."""
    if re.search(r'[?&](?:reply_)?comment_id=',url):return False
    target=urlsplit(source['url']);candidate=urlsplit(url)
    profile=target.path.strip('/').lower();path=candidate.path.strip('/').lower()
    author=urlsplit(row.get('ownerUrl') or '')
    from run import HOSTS
    owner=author.path.strip('/').lower() if author.hostname in HOSTS['facebook'] else ''
    if profile=='profile.php':
        identity=parse_qs(target.query).get('id',[''])[0]
        return bool(identity) and (str(row.get('ownerId',''))==identity or owner==identity or
            (owner=='profile.php' and parse_qs(author.query).get('id')==[identity]) or
            parse_qs(candidate.query).get('id')==[identity] or path.startswith(identity+'/posts/') or path.startswith(identity+'/videos/'))
    if '/posts/' in profile:profile=profile.split('/posts/')[0]
    if profile.startswith('groups/'):
        group='/'.join(profile.split('/')[:2]);return path.startswith(group+'/posts/')
    return bool(profile) and (owner==profile or any(path.startswith(profile+'/'+part+'/') for part in ('posts','videos','permalink','reel','reels')))


def continuation(base, candidate):
    if not candidate:return None
    absolute=urljoin(base,candidate)
    owner=urlsplit(base); link=urlsplit(absolute)
    if (link.scheme!='https' or link.netloc!=owner.netloc or link.username or link.password or
            link.path.rstrip('/')!=owner.path.rstrip('/') or len(absolute)>2048 or link.fragment):return None
    fixed=parse_qs(owner.query); query=parse_qs(link.query)
    paging={'page','cursor','after','before','offset','max_id','start_from','page_token','nextPageToken','st.page','st.offset'}
    if not (paging & query.keys()) or {k:v for k,v in query.items() if k not in paging}!={k:v for k,v in fixed.items() if k not in paging}:return None
    return absolute


def wall_owner(source, row, permalink):
    from run import HOSTS
    platform = source['platform']
    owner = urlsplit(source['url']).path.strip('/').lower()
    author = urlsplit(row.get('ownerUrl') or '')
    author_path = author.path.strip('/').lower() if author.hostname in HOSTS[platform] else ''
    if platform == 'vk':
        club = re.fullmatch(r'(?:public|club)(\d+)', owner)
        wall = re.fullmatch(r'/wall(-?\d+)_\d+', urlsplit(permalink).path)
        return bool(wall and (wall[1] == '-' + club[1] if club else author_path == owner))
    if row.get('privateCard'):
        return False
    path = urlsplit(permalink).path.strip('/').lower()
    group = row.get('groupId')
    if group:
        return owner == 'group/' + group
    return path.rsplit('/', 2)[0] == owner and (not author_path or author_path == owner)


def normalize_rows(source,rows,limit):
    from run import HOSTS, IDENTITIES, post, social_url
    from public_embeds import timestamp
    from candidate_audit import count, reject
    platform=source['platform'];items={}
    for row in rows:
        count('raw_input')
        if not isinstance(row,dict):
            reject('invalid_row');continue
        url=row.get('url','')
        if not isinstance(url,str) or not url.strip():
            reject('missing_permalink');continue
        from urllib.parse import urljoin
        url=urljoin(source['url'],url)
        if urlsplit(url).hostname not in HOSTS[platform]:
            reject('foreign_permalink', url);continue
        match=re.search(IDENTITIES[platform],url)
        if not match:
            reject('invalid_post_identity', url);continue
        if platform in ('vk','ok') and not wall_owner(source,row,url):
            reject('wall_owner_mismatch_or_private', url);continue
        if platform=='facebook':
            from social_links import identity
            if not identity(url) or not facebook_owned(source,row,url):
                reject('unverified_facebook_owner_or_identity',url);continue
            requested=identity(source['url'])
            if requested and requested['canonical_key']!='facebook:'+match[1]:
                reject('different_requested_post',url);continue
        if platform=='x':
            from x_api import target
            resolved=target(source)
            status=re.fullmatch(r'/([A-Za-z0-9_]{1,15})/status/(\d+)/?',urlsplit(url).path)
            requested=re.fullmatch(r'/([A-Za-z0-9_]{1,15})/status/(\d+)/?',urlsplit(source['url']).path)
            if not status or (not resolved and (not requested or requested[1].lower()!=status[1].lower() or requested[2]!=status[2])) or (
                    resolved and resolved[0]=='timeline' and status[1].lower()!=resolved[1].lower()):
                reject('x_source_mismatch',url)
                continue
        if platform in ('threads','tiktok') and urlsplit(url).path.split('/')[1].lower()!=urlsplit(source['url']).path.split('/')[1].lower():
            reject('profile_owner_mismatch',url);continue
        text=str(row.get('text') or '')
        media=(row.get('images') or [])+(row.get('videos') or [])
        if not text.strip() and not media:
            reject('no_public_text_or_media', url);continue
        item=post(source,platform+':'+match[1],social_url(url),text,timestamp(row.get('publishedAt')),media)
        from engagement_metrics import retain_counters
        retain_counters(item,row)
        item.update(capture_mode='visible_public_card',text_may_be_truncated=True)
        if platform=='facebook' and row.get('expanded') and len(text)<=100000:
            item.update(text_may_be_truncated=False,facebook_text_source='expanded_card')
        if platform=='facebook' and item.get('published_at'):
            item['facebook_date_source']='card_timestamp'
        items[item['canonical_key']]=item
        if len(items)>=limit and platform not in ('vk','ok'):break
    return list(items.values())


async def http_status(response):
    if response and response.status in (429,503):
        from datetime import datetime, timezone
        from source_health import note_retry_after
        note_retry_after(await response.header_value('retry-after'),datetime.now(timezone.utc))
    return response.status if response else 0


async def wall_dates(context, source, items, progress, deadline):
    from datetime import datetime, timezone, timedelta
    from run import date, HOSTS, IDENTITIES
    from source_health import instant
    if progress is None:return
    now = datetime.now(timezone.utc)
    prior = progress.get('date_probes') or {}
    probes = {key:stamp for key,stamp in prior.items() if now-instant(stamp)<timedelta(hours=6)}
    candidates = [p for k,p in items.items() if not p.get('published_at') and k not in probes]
    for item in candidates[:2]:
        if deadline-time.monotonic()<9:break
        await asyncio.sleep(2)
        detail=await context.new_page()
        try:
            response=await detail.goto(item['post_url'],wait_until='domcontentloaded',timeout=6000)
            if await http_status(response)>=400:
                if response.status in (401,403,429,503):raise ValueError('http_'+str(response.status))
                continue
            failure=page_error(detail.url, await detail.locator('body').inner_text(timeout=1800))
            if failure:raise ValueError(failure)
            platform=source['platform']
            match=re.search(IDENTITIES[platform],urlsplit(detail.url).path)
            if urlsplit(detail.url).hostname not in HOSTS[platform] or not match or platform+':'+match[1]!=item['canonical_key']:
                continue
            value=await detail.evaluate('''() => {
                const root=document.head;
                for(const selector of ['meta[property="article:published_time"]','meta[itemprop="datePublished"]']){
                    const v=root.querySelector(selector)?.content;
                    if(v)return v;
                }
                return null;
            }''')
            stamp=date(value)
            if stamp and instant(stamp)<=now:
                item['published_at']=stamp
                item['date_source']='public_permalink_metadata'
            probes[item['canonical_key']]=now.isoformat()
        finally:
            await detail.close()
            progress['date_probes']=dict(list(probes.items())[-128:])


async def context_for(browser):
    from run import safe_url
    context=await browser.new_context(locale='uk-UA',service_workers='block')
    async def route(route):
        try:
            if route.request.resource_type in ('media','font','image'):return await route.abort()
            await asyncio.to_thread(safe_url,route.request.url)
            await route.continue_()
        except Exception:await route.abort()
    await context.route('**/*',route)
    return context


def page_error(url,body):
    if '__jhash_' in body and '__jua_' in body:return 'challenge_required'
    if re.search(r'cf-chl-|/cdn-cgi/challenge-platform/|sorry, you have been blocked',body[:65536],re.I):return 'challenge_required'
    if re.search(r'captcha|verify you are human|just a moment|Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° Р В±РЎР‚Р В°РЎС“Р В·Р ВµРЎР‚Р В°',body[:10000],re.I):return 'challenge_required'
    if re.search(r'/(?:login|accounts/login|checkpoint|auth)(?:/|\?|$)',url,re.I):return 'login_required'
    return None


async def collect(browser,source,limit):
    from run import HOSTS, error_code
    import candidate_audit,depth
    platform=source['platform']
    if platform=='facebook':
        from facebook_feed import collect as collect_feed
        items,status,_=await collect_feed(browser,source,limit)
        if not items and status!='no_public_posts':
            raise ValueError(status)
        return items
    if platform in ('max','whatsapp'):raise ValueError('verified_post_identity_unavailable')
    adapter={'facebook':'Facebook','tiktok':'TikTok','instagram':'Instagram','vk':'Vk','ok':'Ok','threads':'Threads','x':'X'}[platform]
    cpp=(Path(__file__).resolve().parents[2]/'backend/src/platforms'/f'{adapter}Adapter.cpp').read_text()
    expression=re.search(r'extractionExpression\s*=\s*R"JS\((.*?)\)JS"',cpp,re.S)
    if not expression:raise ValueError('adapter_expression_missing')
    script=expression[1]
    targets=[source['url']]
    parsed=urlsplit(source['url'])
    from public_surfaces import wall_surface
    surface=wall_surface(source)
    scope=source
    if surface:
        # Use the platform's expressly public feed, not a login-dependent wall.
        targets=[surface[0]]
        scope=surface[1]
    if platform=='x' and parsed.path.rstrip('/')=='/search':
        from x_api import target as x_target
        if not x_target(source):raise ValueError('x_search_query_required')
        query={'q':x_target(source)[1], 'f':'live'}
        targets=[parsed._replace(query=urlencode(query)).geturl()]
    if platform=='vk' and not surface:targets.append(urlunsplit(('https','m.vk.com',parsed.path,parsed.query,'')))
    if platform=='instagram' and re.fullmatch(r'/[A-Za-z0-9_.]+/?',parsed.path):targets.append('https://www.instagram.com/'+parsed.path.strip('/')+'/embed/')
    context=await context_for(browser);items={};last='no_public_posts'
    started=time.monotonic()
    progress=depth.browser_progress.get()
    try:
        page=await context.new_page()
        for target in dict.fromkeys(targets):
            try:
                response=await page.goto(target,wait_until='domcontentloaded',timeout=15000)
                if await http_status(response)>=400:raise ValueError('http_'+str(response.status))
                if page_error(page.url,'') == 'login_required':raise ValueError('login_required')
                if platform in ('vk','ok') and urlsplit(page.url).hostname not in HOSTS[platform]:
                    raise ValueError('unexpected_destination')
                await page.wait_for_timeout(1200)
                body=await page.locator('body').inner_text(timeout=2500)
                failure=page_error(page.url,body)
                if failure:raise ValueError(failure)
                if platform=='x' and parsed.path.rstrip('/')=='/search':
                    if x_target({**source,'url':page.url})!=x_target(source):
                        raise ValueError('unexpected_destination')
                # Collect before each scroll: virtualized feeds remove earlier cards.
                max_pages,_,seconds=depth.policy('browser')
                is_wall = platform in ('vk','ok')
                if is_wall and progress and progress.get('resume'):
                    max_pages = min(max_pages * 2, depth.CONFIG[depth.mode.get()]['target_max_pages_per_source'])
                # Navigation is part of a live poll's budget, not extra time
                # that can let the outer timeout discard already-read cards.
                deadline=(started if depth.bounded_poll.get() else time.monotonic())+seconds
                idle=0
                visited={target}
                for step in range(max_pages):
                    failure=page_error(page.url,await page.locator('body').inner_text(timeout=2500))
                    if failure:raise ValueError(failure)
                    before=len(items)
                    rows=await page.evaluate(script)
                    candidate_audit.count('browser_observations')
                    observed = normalize_rows(scope,rows,len(rows) if platform=='x' else limit)
                    if surface:
                        for item in observed:
                            item['source_url']=source['url']
                            item['capture_mode']='native_public_topics'
                    for item in observed:
                        if platform=='x' and item['canonical_key'] in items:
                            candidate_audit.count('repeat_visible_card')
                        items[item['canonical_key']]=item
                    if is_wall and progress is not None:
                        progress.setdefault('wall_pages', []).append({
                            'url':page.url,'cards':len(rows),'permalinks':list(dict.fromkeys(
                                urlsplit(row['url']).path for row in rows if row.get('url'))),
                            'accepted_keys':[item['canonical_key'] for item in observed]})
                    idle=idle+1 if len(items)==before else 0
                    selector = 'a[rel~=next][href],link[rel~=next][href]'
                    if is_wall:
                        selector += ',a.pg_more[href],a.show_more[href],a[data-l="t,loadMore"][href],a[href*="offset="],a[href*="st.page="]'
                    links=await page.locator(selector).evaluate_all('(els)=>els.map(e=>e.href)')
                    next_url=next((u for link in links if (u:=continuation(target,link)) and u not in visited),None)
                    # Always inspect the current head before resuming a durable backwards link.
                    if progress is not None and step==0:
                        resume=continuation(target,progress.get('resume'))
                        if resume and resume not in visited:next_url=resume
                    if progress is not None and (rows or next_url) and (surface or urlsplit(target).path.rstrip('/')==parsed.path.rstrip('/')):
                        # A cut-off page must be revisited; never checkpoint past unseen cards.
                        progress.update(next_url=continuation(target,page.url) if not is_wall and len(items)>=limit else next_url)
                    if (len(items)>=limit and not is_wall) or time.monotonic()>=deadline:break
                    if next_url:
                        visited.add(next_url)
                        response=await page.goto(next_url,wait_until='domcontentloaded',timeout=9000)
                        if await http_status(response)>=400:raise ValueError('http_'+str(response.status))
                        if page.url!=next_url:raise ValueError('unexpected_destination')
                        idle=0
                        continue
                    if idle>=depth.CONFIG['platforms']['browser']['idle_observations']:break
                    await page.evaluate('window.scrollBy(0,Math.max(window.innerHeight,document.documentElement.clientHeight))')
                    await page.wait_for_timeout(900+400*idle)
                if items:
                    if is_wall:
                        await wall_dates(context,source,items,progress,deadline)
                        return list(items.values())
                    return list(items.values()) if platform=='x' else list(items.values())[:limit]
                last='login_required' if re.search(r'sign in|log in|РЎС“Р Р†РЎвЂ“Р в„–РЎвЂљР С‘|Р Р†Р С•Р в„–РЎвЂљР С‘',body,re.I) else 'unverified_markup'
            except Exception as error:
                last=error_code(error)
                if items and last not in ('http_401','http_403','http_429','challenge_required','login_required'):
                    candidate_audit.count('browser_stop_'+last)
                    return list(items.values())
                if last in ('http_401','http_403','http_429','challenge_required','login_required'):
                    if items:
                        candidate_audit.count('browser_stop_'+last)
                        return list(items.values()) if platform in ('vk','ok') else list(items.values())[:limit]
                    raise ValueError(last) from None
        if last!='no_public_posts':raise ValueError(last)
        return []
    finally:await context.close()


async def website(browser,source,limit):
    """Render JavaScript news sites using the same dated-article acceptance rules."""
    from run import feed_posts
    from article_enrichment import row_post
    from web_pages import article_rows,article_links
    context=await context_for(browser)
    try:
        page=await context.new_page()
        async def read(url):
            response=await page.goto(url,wait_until='domcontentloaded',timeout=15000)
            if await http_status(response)>=400:raise ValueError('http_'+str(response.status))
            await page.wait_for_timeout(800)
            body=await page.content()
            if len(body)>8*1024**2:raise ValueError('response_too_large')
            error=page_error(page.url,await page.locator('body').inner_text(timeout=2000))
            if error:raise ValueError(error)
            return body,page.url
        body,url=await read(source['url']);feed=feed_posts(source,body,limit)
        if feed:return feed
        rows=article_rows(body,url)
        if not rows:
            for link in article_links(body,url,min(limit,3)):
                try:
                    body,final=await read(link);rows.extend(article_rows(body,final))
                except ValueError as error:
                    if str(error) in ('http_429','challenge_required'):raise
                    continue
        items=[]
        for row in rows[:limit]:
            item=row_post(source,row)
            item['rendered_by']='playwright';items.append(item)
        return items
    finally:await context.close()
