"""Replay the unvisited part of a frozen Facebook candidate queue."""
import asyncio
from datetime import datetime
from urllib.parse import urlsplit

from browser_public import normalize_rows
from facebook_feed import CARDS, combine, context_for, story_post
from facebook_older import candidate, page_data, strictly_older
from run import error_code


def legacy_tail(source, plan):
    """Prove the ordered suffix from the saved seeds and the actual read log."""
    sid = source['id']
    reads = source['reads']
    keys = [r['candidate'] for r in reads]
    if source['stages'].get('detail') != 'candidate_budget' or len(keys) != 20 or len(set(keys)) != 20:
        raise ValueError('not_a_20_link_checkpoint')
    start = next((i for i,r in enumerate(reads) if r['via']=='archive_permalink'), len(reads))
    if start == len(reads) or any(r['via']!='archive_permalink' for r in reads[start:]):
        raise ValueError('unproven_archive_suffix')
    skip = set(keys[:start])
    anchor = source.get('anchor')
    if anchor and anchor['basis'] != 'newest_verified_candidate':
        skip.add(anchor['key'])
    entries = {}
    for url in plan['seeds'][sid]:
        f = candidate(url)
        if f and f['canonical_key'] not in skip:
            entries.setdefault(f['canonical_key'], {'key':f['canonical_key'],'url':f['url'],'via':'archive_permalink'})
    ordered = list(entries)
    if ordered[:20-start] != keys[start:] or start+len(entries) != source['candidates']:
        raise ValueError('saved_reads_do_not_match_original_queue')
    # Media pages can append parent-story links at runtime. The old run did
    # not checkpoint those URLs. Only reconstruct if none could have done so.
    for r in reads:
        entry = entries.get(r['candidate'])
        media = r['via']=='photo_tab_link' or (entry and any(t in urlsplit(entry['url']).path for t in ('/photo','/videos/','/watch')))
        if media and r['status'] not in ('login_required','http_403','http_401','http_404'):
            raise ValueError('unproven_dynamic_media_suffix')
    return list(entries.values())[20-start:]


async def resume(browser, plan):
    sources = plan['sources']
    # Each source consumes its own queue in its original order. Share only
    # this run's anonymous response when several source queues have one URL.
    cache = {}
    blocked = False
    records, posts = [], {}
    for source in sources:
        sid = source['id']
        entries = plan['remaining'][sid]
        anchor = plan['anchors'].get(sid)
        record = {'id':sid,'name':source['name'],'url':source['url'],'anchor':anchor,'reads':[],
                  'candidates':len(entries),'start_position':21,'stages':{},'accepted':[]}
        items = {}
        for entry in entries:
            key = entry['key']
            if key not in cache:
                if blocked:
                    break
                if cache:
                    await asyncio.sleep(1)
                context = await context_for(browser)
                try:
                    page = await context.new_page()
                    nodes,_,parents = await page_data(page,entry['url'],True)
                    cards = await page.evaluate(CARDS)
                    cache[key] = (nodes,parents,cards,'public_response')
                except Exception as e:
                    status = error_code(e)
                    cache[key] = ([],[],[],status)
                    blocked = status in ('http_429','challenge_required')
                finally:
                    await context.close()
            nodes,parents,cards,status = cache[key]
            matches = []
            if status=='public_response':
                for node in nodes:
                    row=story_post(source,node)
                    if row and key in (row['canonical_key'],'facebook:'+row['facebook_story_id']):
                        matches.append(row)
                matches.extend(r for node in parents if (r:=story_post(source,node)))
                matches.extend(r for r in normalize_rows(source,cards,max(1,len(cards))) if r['canonical_key']==key)
                combine(items,matches)
                status='verified_identity' if matches else 'no_verified_owned_post'
            record['reads'].append({'candidate':key,'via':entry['via'],'status':status})
        dated = [r for r in items.values() if r.get('published_at') and (r['text'].strip() or r['media_urls'])]
        if anchor is None and dated:
            head = max(dated,key=lambda r:datetime.fromisoformat(r['published_at']))
            anchor = record['anchor'] = {'key':head['canonical_key'],'date':head['published_at'],
                                         'story_id':head.get('facebook_story_id'),'basis':'newest_verified_candidate'}
        for row in dated:
            if not strictly_older(row,anchor):
                continue
            row.update(facebook_older_method='archive_permalink',facebook_before_key=anchor['key'],
                       facebook_before_date=anchor['date'],facebook_known_before=(
                           row['canonical_key'] in plan['known'] or row.get('facebook_story_id') in plan['known_native']))
            record['accepted'].append(row['canonical_key'])
            combine(posts,[row])
        record['remaining_queue'] = entries[len(record['reads']):]
        record['stages']['detail'] = 'rate_or_challenge_stop' if record['remaining_queue'] else 'queue_exhausted'
        records.append(record)
    return records,list(posts.values()),len(cache)
