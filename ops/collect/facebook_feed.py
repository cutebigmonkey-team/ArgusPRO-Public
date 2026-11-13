"""Read Facebook's public story objects and rendered cards in an anonymous browser."""
import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from browser_public import context_for, facebook_owned, normalize_rows, page_error
from public_embeds import timestamp
from run import HOSTS, error_code, post
from social_links import identity

MAX_RESPONSE = 8 * 1024 * 1024
BLOCKS = {'http_401', 'http_403', 'http_429', 'login_required', 'challenge_required'}
CARDS = Path(__file__).with_name('facebook_cards.js').read_text(encoding='utf-8')
ADVANCE = """() => {
  const feed = document.querySelector('[role=feed]') || document;
  const controls = feed.querySelectorAll('button,[role=button]');
  const next = Array.from(controls).find(el => !el.closest('article,[role=article],a[href]') &&
    /^(older posts|show more posts|see more posts|попередні дописи|показати більше дописів|більше дописів)$/iu.test(el.innerText.trim()));
  if (next) { next.click(); return; }
  let scroller = document.querySelector('article,[role=article]')?.parentElement;
  while (scroller && !(scroller.clientHeight && scroller.scrollHeight > scroller.clientHeight + 20 &&
    /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
  (scroller || document.scrollingElement).scrollTop = (scroller || document.scrollingElement).scrollHeight;
}"""
FRESH_SCRIPTS = """() => {
  const seen = window.argusSeenScripts ||= new WeakSet();
  return Array.from(document.querySelectorAll('script[type="application/json"]')).filter(s => {
    if (seen.has(s)) return false;
    seen.add(s);
    return s.textContent.length <= 8388608 && /timeline_list_feed_units|group_feed|node_v2/.test(s.textContent);
  }).map(s => s.textContent);
}"""


def objects(raw):
    """Facebook can send several JSON envelopes separated by newlines."""
    for line in raw.removeprefix('for (;;);').splitlines():
        if line.lstrip().startswith(('{', '[')):
            try:
                yield json.loads(line)
            except (ValueError, RecursionError):
                continue


def extract(payload, detail=False):
    stories, flags = [], []
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(reversed(value))
        elif isinstance(value, dict):
            path = value.get('path')
            if isinstance(path,list) and any(k in path for k in ('timeline_list_feed_units','group_feed')) and 'edges' in path:
                edge = value.get('data')
                if isinstance(edge,dict) and isinstance(edge.get('node'),dict):
                    stories.append(edge['node'])
            for key, child in value.items():
                if key in ('timeline_list_feed_units', 'group_feed') and isinstance(child, dict):
                    stories.extend(edge['node'] for edge in child.get('edges') or []
                                   if isinstance(edge, dict) and isinstance(edge.get('node'), dict))
                    info = child.get('page_info')
                    more = info.get('has_next_page') if isinstance(info,dict) else None
                    if isinstance(more, bool):
                        flags.append(more)
                elif detail and key == 'node_v2' and isinstance(child, dict):
                    stories.append(child)
                elif key not in ('comments', 'feedback', 'attachments', 'attached_story', 'suggested_content'):
                    if isinstance(child, (dict, list)):
                        stack.append(child)
    return stories, flags


def story_post(source, story, required_key=None):
    from run import date
    def branch(node,*fields):
        for field in fields:
            node=node.get(field) if isinstance(node,dict) else None
        return node if isinstance(node,dict) else {}
    if not isinstance(story,dict):return None
    content = branch(story,'comet_sections','content','story')
    timed = branch(story,'comet_sections','timestamp','story')
    post_id = str(story.get('post_id') or '')
    post_id = post_id or str(content.get('post_id') or '')
    if not post_id.isdigit() or str(content.get('post_id') or post_id) != post_id:
        return None
    message_story = branch(content,'comet_sections','message','story')
    if str(message_story.get('post_id') or post_id) != post_id:
        return None
    actors = content.get('actors') or story.get('actors') or timed.get('actors') or []
    author = actors[0] if isinstance(actors,list) and actors and isinstance(actors[0], dict) else {}
    owner = {'ownerId': str(author.get('id', '')), 'ownerUrl': author.get('url') or ''}
    for url in (content.get('wwwURL'), timed.get('url'), message_story.get('url'), story.get('url')):
        found = identity(url)
        if not found or found['platform'] != 'facebook':
            continue
        link_id=found['canonical_key'].partition(':')[2]
        if link_id.isdigit() and link_id!=post_id:
            continue
        if required_key and found['canonical_key'] != required_key:
            continue
        if not required_key and not facebook_owned(source, owner, url):
            continue
        break
    else:
        return None
    message = content.get('message') or message_story.get('message') or story.get('message') or {}
    text = message.get('text') if isinstance(message, dict) else None
    published = None
    for part in (story, content, timed, message_story):
        if str(part.get('post_id') or post_id) != post_id:
            continue
        stamp = str(part.get('creation_time') or '')
        if re.fullmatch(r'\d{10}', stamp) and 1072915200 <= int(stamp) <= time.time()+86400:
            published = date(timestamp(stamp))
            break
    # A textless story may still supply a date for the matching rendered media card.
    full = isinstance(text, str) and bool(text.strip()) and len(text) <= 100000 and not re.search(
        r'(?:…|\.\.\.)\s*(?:see more|більше|показати більше)?\s*$',text,re.I) and not any(
        obj.get('is_truncated') is True for obj in (story, content, message_story, message) if isinstance(obj, dict))
    media=[]
    for attachment in (content.get('attachments') or story.get('attachments') or [])[:12]:
        if not isinstance(attachment,dict):continue
        asset=branch(attachment,'styles','attachment','media') or branch(attachment,'media')
        for image_name in ('image','thumbnailImage','preview_image'):
            uri=branch(asset,image_name).get('uri')
            if isinstance(uri,str):media.append(uri)
    row = post(source, found['canonical_key'], found['url'], text if isinstance(text, str) else '', published, media)
    row.update(capture_mode='facebook_public_story', text_may_be_truncated=not full,
               facebook_text_source='story_message' if full else 'unverified', facebook_story_id=post_id)
    if owner['ownerId'].isdigit():row['platform_post_id']=owner['ownerId']+'_'+post_id
    if published:
        row['facebook_date_source'] = 'story_creation_time'
    comments = []
    feedback = story.get('feedback') if isinstance(story.get('feedback'),dict) else {}
    feedback = feedback or branch(content,'feedback') or branch(story,'comet_sections','feedback','story','feedback')
    metrics = {}
    for name, candidates in {
        'likes':(branch(feedback,'reaction_count').get('count'),),
        'comments':(branch(feedback,'comment_count').get('total_count'),branch(feedback,'comments').get('total_count')),
        'shares':(branch(feedback,'share_count').get('count'),),
    }.items():
        value = next((v for v in candidates if type(v) is int and v >= 0), None)
        if value is not None: metrics[name] = value
    if metrics: row['engagement'] = metrics
    connection = feedback.get('display_comments') or feedback.get('comments') or {}
    edges = connection.get('edges') if isinstance(connection,dict) else None
    for edge in edges[:20] if isinstance(edges,list) else []:
        node = edge.get('node') if isinstance(edge,dict) else None
        if not isinstance(node,dict) or not node.get('id') or not isinstance(node.get('body'),dict):
            continue
        body=node['body'].get('text')
        if isinstance(body,str) and body.strip():
            comments.append({'id':str(node['id'])[:200], 'text':body[:4000]})
    if comments:
        row['public_comments']=comments
    return row


def combine(items, incoming):
    for row in incoming:
        key = row['canonical_key']
        old = items.get(key)
        if old is None:
            items[key] = row
            continue
        if row.get('facebook_text_source') == 'story_message' or (row.get('facebook_text_source')=='expanded_card' and old.get('text_may_be_truncated')) or (not old['text'] and row['text']):
            for field in ('text', 'text_may_be_truncated', 'capture_mode', 'facebook_text_source'):
                if field in row:
                    old[field] = row[field]
        if row.get('published_at'):
            old['published_at'] = row['published_at']
            old['facebook_date_source'] = row.get('facebook_date_source', 'card_timestamp')
        if row.get('facebook_story_id'):
            old['facebook_story_id'] = row['facebook_story_id']
        if row.get('platform_post_id'):
            old['platform_post_id'] = row['platform_post_id']
        old['media_urls'] = list(dict.fromkeys(old['media_urls'] + row['media_urls']))[:12]
        if row.get('public_comments'):
            old['public_comments'] = list({c['id']:c for c in old.get('public_comments',[])+row['public_comments']}.values())[:20]


def owned_story_links(source, links):
    from facebook_older import candidate
    found = {}
    for link in links:
        story = candidate(link)
        if story and facebook_owned(source, {}, story['url']):
            found.setdefault(story['canonical_key'], story['url'])
    return found


async def collect(browser, source, limit):
    context = await context_for(browser)
    items, stops = {}, []
    import depth
    progress = depth.browser_progress.get()
    prior = progress.get('facebook_pending', []) if progress is not None else []
    discovered = owned_story_links(source, prior)
    started = time.monotonic()
    # Leave time to return verified cards before the supervisor's source timeout.
    deadline = started + (min(80, depth.policy('browser')[2]) if depth.bounded_poll.get() else 80)
    if depth.bounded_poll.get() and depth.source_deadline.get() is not None:
        # Browser launch and earlier readers consumed part of the outer budget.
        # Stop extraction in time to return already verified cards and close it.
        deadline=min(deadline,depth.source_deadline.get()-2)
    diagnostics = {'scrolls':0, 'feed_responses':0, 'permalinks_read':0, 'raw_rows':0}
    from collections import Counter
    exclusions=Counter()
    status = 'no_public_posts'
    scope = source

    async def read_surface(url, label, required_key=None):
        nonlocal scope
        page = await context.new_page()
        responses = []
        seen_here = set()
        def observe(response):
            u = urlsplit(response.url)
            if u.hostname in HOSTS['facebook'] and u.path.rstrip('/') in ('/api/graphql','/graphql'):
                responses.append(response)
        page.on('response', observe)
        async def consume(raw):
            for payload in objects(raw):
                stories, flags = extract(payload, detail=bool(required_key))
                diagnostics['raw_rows']+=len(stories)
                # A new link has to prove ownership on the detail page too.
                bound = required_key if required_key in items else None
                rows = [r for node in stories if (r:=story_post(scope, node, bound))
                        and (not required_key or r['canonical_key'] == required_key)]
                exclusions['unverified_story_identity_or_owner']+=len(stories)-len(rows)
                seen_here.update(r['canonical_key'] for r in rows)
                combine(items, rows)
                if not required_key:
                    page_flags.extend(flags)
        page_flags = []
        try:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=14000)
            if response and response.status >= 400:
                return 'http_' + str(response.status)
            final = urlsplit(page.url)
            if final.hostname not in HOSTS['facebook']:
                return 'unexpected_destination'
            if page_error(page.url, ''):
                return 'login_required'
            if label=='timeline' and urlsplit(source['url']).path.startswith('/share/') and re.fullmatch(r'/(?:groups/)?[\w.-]+/?', final.path):
                scope = {**source, 'url':page.url}
            await page.wait_for_timeout(850)
            idle = 0
            while time.monotonic() < deadline:
                before = len(items)
                body = await page.locator('body').inner_text(timeout=2500)
                failure = page_error(page.url, body)
                if failure:
                    return failure
                for raw in await page.evaluate(FRESH_SCRIPTS):
                    await consume(raw)
                batch = responses.copy()
                responses.clear()
                for response in batch:
                    if response.status in (401,403,429):
                        return 'http_' + str(response.status)
                    size = await response.header_value('content-length')
                    if size and size.isdigit() and int(size)>MAX_RESPONSE:
                        continue
                    try:
                        raw = await asyncio.wait_for(response.body(), 2)
                    except Exception:
                        continue
                    if len(raw) <= MAX_RESPONSE:
                        await consume(raw.decode(errors='replace'))
                        diagnostics['feed_responses'] += 1
                cards = await page.evaluate(CARDS)
                diagnostics['raw_rows']+=len(cards)
                rows = normalize_rows(scope, cards, max(1,len(cards)))
                if required_key:
                    rows = [r for r in rows if r['canonical_key']==required_key]
                exclusions['unmatched_or_repeated_rendered_card']+=len(cards)-len(rows)
                seen_here.update(r['canonical_key'] for r in rows)
                combine(items, rows)
                if required_key:
                    return 'permalink_read'
                if label in ('timeline','posts_tab'):
                    links = await page.locator('a[href]').evaluate_all('(a)=>a.map(n=>n.href)')
                    discovered.update(owned_story_links(scope, links))
                if page_flags and page_flags[-1] is False:
                    return 'server_end'
                if len(items) >= limit:
                    return 'snapshot_limit'
                idle = idle+1 if len(items)==before else 0
                if idle>=3:
                    if page_flags and page_flags[-1]:
                        return 'more_advertised_without_progress'
                    return 'no_further_visible_posts' if seen_here else 'login_prompt' if re.search(r'log in|увійти|войти',body,re.I) else 'no_visible_posts'
                await page.evaluate(ADVANCE)
                diagnostics['scrolls'] += 1
                await page.wait_for_timeout(1000)
            return 'time_budget'
        finally:
            await page.close()

    async def bounded(url,label,key=None):
        try:
            return await asyncio.wait_for(read_surface(url,label,key),max(.1,deadline-time.monotonic()))
        except Exception as error:
            return error_code(error)

    try:
        from facebook_older import source_tab
        surfaces=[('timeline',source['url'])]
        posts_tab = source_tab(source['url'], 'posts')
        if posts_tab and not identity(source['url']):
            surfaces.append(('posts_tab', posts_tab))
            if '/groups/' not in source['url']:
                surfaces.append(('photos_tab', source_tab(source['url'], 'photos')))
        if '/groups/' not in source['url'] and not identity(source['url']):
            surfaces.append(('page_plugin','https://www.facebook.com/plugins/page.php?'+urlencode(
                {'href':source['url'],'tabs':'timeline','width':500,'height':800})))
        inspected=set()
        exhausted=set()
        for label,url in surfaces:
            if label == 'photos_tab':
                if len(items) >= limit:
                    continue
                from facebook_older import candidate, page_data
                page = await context.new_page()
                try:
                    async with asyncio.timeout(max(.1, min(24, deadline-time.monotonic()))):
                        nodes, links, _ = await page_data(page, url)
                        diagnostics['raw_rows']+=len(nodes)
                        combine(items, [r for n in nodes if (r := story_post(scope, n))])
                        photos = dict.fromkeys(c['url'] for link in links if (c := candidate(link))
                                              and c['url'].startswith('https://www.facebook.com/photo/'))
                        for photo in list(photos)[:3]:
                            if len(items) >= limit:
                                break
                            _, _, parents = await page_data(page, photo, detail=True)
                            diagnostics['raw_rows']+=len(parents)
                            combine(items, [r for n in parents if (r := story_post(scope, n))])
                            discovered.update(owned_story_links(scope, [n.get('url') or n.get('wwwURL') for n in parents]))
                        outcome = 'verified_media_parents' if photos else 'no_media_links'
                except Exception as error:
                    outcome = error_code(error)
                finally:
                    await page.close()
            else:
                outcome=await bounded(url,label)
            stops.append({'surface':label,'stop':outcome})
            if outcome in BLOCKS or outcome in ('unexpected_destination','time_budget','timeout'):
                break
            targets = {k:v for k,v in discovered.items() if k not in items and len(items)<limit}
            targets.update({k:r['post_url'] for k,r in items.items()
                            if not (r['text'] and not r['text_may_be_truncated'] and r['published_at'])})
            for key,permalink in targets.items():
                if time.monotonic()>=deadline or len(inspected)>=min(limit,12):
                    break
                if key in inspected:
                    continue
                inspected.add(key)
                diagnostics['permalinks_read']+=1
                outcome=await bounded(permalink,'permalink',key)
                if outcome in ('http_404','http_410') and key not in items:
                    exhausted.add(key)
                    exclusions['unavailable_or_unverified_permalink'] += 1
                if outcome in BLOCKS:
                    stops.append({'surface':'permalink','stop':outcome})
                    break
            if stops[-1]['stop'] in BLOCKS or len(items)>=limit:
                break
    finally:
        await context.close()
    posts=[r for r in items.values() if r['text'].strip() or r['media_urls']]
    exclusions['no_public_content']+=len(items)-len(posts)
    diagnostics['rejected_rows']={k:v for k,v in exclusions.items() if v}
    # The per-surface budget limits browsing, not already-verified observations.
    # Revalidate all persistent permalinks on restore and again on the detail page.
    if progress is not None:
        complete = {row['canonical_key'] for row in posts}
        pending = {key:url for key,url in discovered.items() if key not in complete and key not in exhausted}
        # Rotate attempted unresolved URLs behind links not yet fetched.
        remaining = list(owned_story_links(source,[pending[key] for key in sorted(pending,key=inspected.__contains__)]).values())
        exclusions['unpersistable_owner_alias'] += len(pending)-len(remaining)
        if len(remaining)<=2048:
            progress['facebook_remaining'] = remaining
        else:
            diagnostics['frontier_overflow'] = len(remaining)
            exclusions['frontier_overflow'] += len(remaining)
    if progress is None:posts=posts[:limit]
    for row in posts:
        row['source_url']=source['url']
    outcomes=[s['stop'] for s in stops]
    status='http_429' if 'http_429' in outcomes else 'ok' if posts else next((v for v in outcomes if v in BLOCKS),None)
    if not status:
        status='ok' if posts else 'login_required' if 'login_prompt' in outcomes else next(
            (v for v in outcomes if v in ('timeout','time_budget','collection_failed','unexpected_destination')),
            'no_public_posts' if 'server_end' in outcomes else 'unverified_markup')
    diagnostics.update(tool='facebook_public_feed', status=status,posts=len(posts),surfaces=stops,
                       distinct_story_ids=len({r['facebook_story_id'] for r in posts if r.get('facebook_story_id')}),
                       full_text_with_date=sum(bool(r['text'] and not r['text_may_be_truncated'] and r['published_at']) for r in posts),
                       with_date=sum(bool(r['published_at']) for r in posts))
    return posts,status,[diagnostics]
