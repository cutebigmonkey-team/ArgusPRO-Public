"""Explicit full-source scope, never a sample silently presented as coverage."""
import argparse
import collections
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from pathlib import Path

REQUESTED={'telegram','instagram','facebook','threads','vk','ok','max','tiktok','x'}


def publisher_copy_available(source,cooling):
    return source['platform']=='tiktok' and 'tiktok' in cooling and any(
        ref.get('kind')=='publisher_embed' for ref in source.get('references',[]))


def cooling_hosts(catalog, state, now):
    from source_health import LIMITED, instant
    cooling = set()
    for source in catalog['sources']:
        for section in ('incremental_polling','polling','history_polling'):
            entry = state.get(section,{}).get(source['id'],{})
            if entry.get('url') != source['url']:continue
            if entry.get('collector_status') not in LIMITED and not entry.get('host_cooldown_until'):continue
            # Legacy next_check_at backs off this individual source for up to
            # seven days. Host-wide throttling has its own timer.
            until=entry.get('host_cooldown_until')
            if not until and entry.get('last_checked_at'):
                until=(instant(entry['last_checked_at'])+timedelta(hours=1)).isoformat()
            until=until or entry.get('next_check_at')
            if until and instant(until)>now:
                cooling.add(urlsplit(source['url']).hostname if source['platform'] in ('rss','mailru') else source['platform'])
    return cooling


def due_batch(catalog, state, enabled, now, budget=36):
    """Start with confirmed news, represent due platforms, then drain the oldest backlog."""
    from source_health import instant, BLOCKED, LIMITED
    from news_order import SIGNAL_WINDOW
    cooling = cooling_hosts(catalog,state,now)
    due = []
    for source in catalog['sources']:
        if not source.get('enabled', True) or source['platform'] not in enabled:
            continue
        host = urlsplit(source['url']).hostname if source['platform'] in ('rss','mailru') else source['platform']
        publisher_only=publisher_copy_available(source,cooling)
        if host in cooling and not publisher_only:
            continue
        key = source['id']
        last = state.get('incremental_polling', {}).get(key, {})
        if last.get('url') != source['url']:
            last = {}
        other=[state.get(k,{}).get(key,{}) for k in ('polling','history_polling')]
        if not publisher_only and any(r.get('url')==source['url'] and r.get('collector_status') in BLOCKED|LIMITED|{'robots_challenge_required'}
               and r.get('next_check_at') and instant(r['next_check_at'])>now for r in other):continue
        when = last.get('next_check_at')
        if not when or instant(when) <= now:
            due.append((instant(when).timestamp() if when else 0, key, source))
    due.sort(key=lambda row: row[:2])
    signals=state.get('first_news',{})
    published=[]
    for _,sid,source in due:
        signal=signals.get(sid,{})
        if signal.get('url')==source['url'] and signal.get('published_at'):
            stamp=instant(signal['published_at'])
            if 0<=(now-stamp).total_seconds()<=SIGNAL_WINDOW.total_seconds():published.append((stamp,sid,source))
    first=[s for _,_,s in sorted(published)[:max(1,budget//2)]]
    if not first:
        first=[s for _,_,s in due if s['platform'] in ('telegram','rss','youtube','reddit','mailru')][:1]
    selected={s['id'] for s in first}
    # Reserve a bounded share for due sources with an independently dated recent head.
    # 'first_news' alone vanishes as soon as an ordinary poll acknowledges that head.
    hot_slots = min(budget // 4, max(0, budget // 2 - len(first)))
    for _, sid, source in due:
        if hot_slots == 0:
            break
        last = state.get('incremental_polling', {}).get(sid, {})
        stamp = last.get('last_post_published_at') if last.get('url') == source['url'] else None
        if sid not in selected and stamp and timedelta(0) <= now - instant(stamp) <= timedelta(minutes=30):
            first.append(source)
            selected.add(sid)
            hot_slots -= 1
    # Prefer an independently accessible, already configured channel of the same
    # catalog publisher when its website has an access wall. Keep original attribution.
    def publisher(source):
        return (source.get('origin'), ' '.join(source.get('name','').casefold().split()))
    protected = collections.defaultdict(list)
    for site in catalog['sources']:
        if site['platform'] not in ('rss','mailru') or not site.get('enabled',True):
            continue
        checks = [state.get(section,{}).get(site['id'],{}) for section in ('polling','incremental_polling')]
        checks = [r for r in checks if r.get('url')==site['url'] and r.get('last_checked_at')]
        recent = max(checks,key=lambda r:instant(r['last_checked_at']),default={})
        if recent.get('collector_status') in ('robots_challenge_required','robots_denied','http_403','challenge_required'):
            protected[publisher(site)].append(site['id'])
    alternatives = {}
    for _, sid, candidate in due:
        name = publisher(candidate)
        if name[0] not in ('general','dsns') or not name[1] or candidate['platform'] in ('rss','mailru'):
            continue
        if name not in protected:continue
        if sid not in selected and len(first)<budget//2 and len(alternatives)<2:
            first.append(candidate)
            selected.add(sid)
        if sid in selected:alternatives[sid]=protected[name]
    groups=collections.defaultdict(collections.deque)
    for _,sid,source in due:
        if sid not in selected:
            groups[source['platform']].append(source)
    # Keep the oldest first. When both tabs have due sources on this platform,
    # give the other publisher group a turn before repeating the first.
    for queue in groups.values():
        rest=list(queue)[1:]
        origin=queue[0].get('origin')
        other=[s for s in rest if s.get('origin') in ('general','dsns') and s['origin']!=origin]
        candidates=other or rest
        if not candidates:continue
        choice=candidates[0]
        for source in candidates:
            checks=(state.get(section,{}).get(source['id'],{}) for section in ('incremental_polling','polling'))
            if any(r.get('url')==source['url'] and r.get('discovered_posts',0)>0 for r in checks):
                choice=source
                break
        queue.remove(choice)
        queue.insert(1,choice)
    ordered=first[:budget]
    slots=collections.Counter(s['platform'] for s in ordered)
    origin_slots = collections.Counter((s['platform'], s.get('origin')) for s in ordered)
    while groups and len(ordered)<budget:
        # Ties follow the oldest eligible source in the original queue.
        platform=min(groups,key=slots.__getitem__)
        queue = groups[platform]
        source = queue[0]
        origin = source.get('origin')
        if origin in ('general', 'dsns'):
            other = 'dsns' if origin == 'general' else 'general'
            if origin_slots[platform, other] < origin_slots[platform, origin]:
                source = next((s for s in queue if s.get('origin') == other), source)
        queue.remove(source)
        ordered.append(source)
        origin_slots[platform, source.get('origin')] += 1
        slots[platform]+=1
        if not groups[platform]:
            del groups[platform]
    return {**catalog,'sources':ordered,'cooling_platforms':sorted(cooling),'live_planned_at':now.isoformat(),'live_order':[s['id'] for s in ordered],
            'publisher_alternatives':alternatives, 'news_signals':{s['id']:signals[s['id']] for _,_,s in sorted(published) if s['id'] in selected}}


FEED_PLATFORMS = {'telegram', 'rss', 'mailru'}


def live_platforms(enabled, lane):
    if lane == 'feeds':
        return enabled & FEED_PLATFORMS
    if lane == 'platforms':
        return enabled - FEED_PLATFORMS
    return enabled


def recent_live_plan(state,now,lane=None):
    from source_health import instant
    stamp=state.get('last_live_planned_at') or state.get('last_live_commit_at')
    if lane in ('feeds', 'platforms'):
        stamp = state.get('live_plans', {}).get(lane)
    cooldown = timedelta(minutes=1 if lane=='feeds' else 4)
    return bool(stamp and timedelta(0)<=now-instant(stamp)<cooldown)


def history_batch(catalog,state,enabled,now,per_platform=2,source_id=None):
    from source_health import instant, BLOCKED, LIMITED
    cooling=cooling_hosts(catalog,state,now)
    groups=collections.defaultdict(list)
    exclusions=collections.Counter()
    for source in catalog['sources']:
        if source_id and source['id']!=source_id:continue
        platform=source['platform']
        if platform not in enabled or not source.get('enabled',True):continue
        host=urlsplit(source['url']).hostname if platform in ('rss','mailru') else platform
        publisher_only=publisher_copy_available(source,cooling)
        if host in cooling and not publisher_only:
            exclusions[platform+':host_cooldown']+=1
            continue
        checks=[state.get(k,{}).get(source['id'],{}) for k in ('polling','incremental_polling')]
        checks=[r for r in checks if r.get('url')==source['url']]
        if not publisher_only and any(r.get('collector_status') in BLOCKED|LIMITED and r.get('next_check_at') and instant(r['next_check_at'])>now for r in checks):continue
        prior=state.get('history_polling',{}).get(source['id'],{})
        if prior.get('url')!=source['url']:prior={}
        if prior.get('next_check_at') and instant(prior['next_check_at'])>now:continue
        productive=any(r.get('archived_posts',0)>0 and r.get('last_success_at') for r in checks)
        groups[platform].append((prior.get('last_checked_at',''),source['id'],source,productive))
    chosen=[]
    for group in groups.values():
        oldest=sorted(group)
        productive=next((r for r in oldest if r[3]),None) if per_platform>1 else None
        selected=([productive] if productive else [])+[r for r in oldest if r!=productive]
        chosen.extend(row[2] for row in selected[:per_platform])
    return {**catalog,'sources':chosen,'collection_scope':'bounded historical collection',
            'cooling_platforms':sorted(cooling),'exclusions':dict(exclusions)}


def select(catalog,requested_only=False,facebook_only=False):
    if facebook_only:
        return {**catalog,'sources':[s for s in catalog['sources'] if s['platform']=='facebook'],
                'platform_scope':['facebook'],'collection_scope':'all supplied Facebook sources'}
    selected={**catalog,'sources':[s for s in catalog['sources'] if not requested_only or s['platform'] in REQUESTED]}
    selected['collection_scope']='all supplied sources for requested social platforms' if requested_only else 'all supplied platform sources'
    if requested_only: selected['platform_scope']=sorted(REQUESTED)
    else: selected.pop('platform_scope',None)
    return selected


if __name__=='__main__':
    p=argparse.ArgumentParser();group=p.add_mutually_exclusive_group()
    group.add_argument('--requested-only',action='store_true')
    group.add_argument('--facebook-only',action='store_true')
    group.add_argument('--incremental',action='store_true')
    group.add_argument('--backfill',action='store_true')
    p.add_argument('--timer',action='store_true')
    p.add_argument('--lane', choices=('all','feeds','platforms','focused'), default='all')
    group.add_argument('--reviewed-social',action='store_true')
    from registry import ENGINES
    p.add_argument('--platform',choices=('all','meta',*ENGINES),default='all')
    a=p.parse_args()
    catalog=select(json.loads(Path('ops/collect/sources.json').read_text(encoding='utf-8')),a.requested_only,a.facebook_only)
    root=Path('registry');root.mkdir(exist_ok=True)
    dynamic=root/'discovered-sources.json'
    if (a.incremental or a.backfill) and dynamic.is_file():
        from source_discovery import add_registered
        catalog=add_registered(catalog,json.loads(dynamic.read_bytes()))
    if a.reviewed_social:
        from import_sheet import is_verified_profile
        state=json.loads((root/'cursors.json').read_bytes())
        cooling=cooling_hosts(catalog,state,datetime.now(timezone.utc))
        platforms={'facebook','instagram'} if a.platform=='meta' else {a.platform}
        profiles=[s for s in catalog['sources'] if is_verified_profile(s) and s.get('enabled',True)
                  and ('all' in platforms or s['platform'] in platforms)]
        catalog={**catalog,'sources':profiles,'collection_scope':'verified general social profiles',
                 'platform_scope':sorted({s['platform'] for s in profiles}),
                 'cooling_platforms':sorted(cooling & {s['platform'] for s in profiles})}
    if a.incremental or a.backfill:
        from registry import validate
        registry = validate(json.loads((root/'collectors.json').read_bytes()))
        enabled = {r['platform'] for r in registry['collectors'] if r['enabled']}
        state=json.loads((root/'cursors.json').read_bytes())
        clock=datetime.now(timezone.utc)
        if a.backfill:
            platforms=enabled if a.platform=='all' else enabled.intersection({'facebook','instagram'} if a.platform=='meta' else {a.platform})
            catalog=history_batch(catalog,state,platforms,clock,source_id=os.getenv('BACKFILL_SOURCE'))
        elif os.getenv('BACKFILL_SOURCE') or os.getenv('BACKFILL_URL'):
            from url_identity import same_site
            from source_health import LIMITED, instant
            source=next((s for s in catalog['sources'] if s['id']==os.getenv('BACKFILL_SOURCE')),None)
            target=os.getenv('BACKFILL_URL','')
            if not source or not source.get('enabled',True) or source['platform'] not in enabled.intersection({'rss','mailru','youtube'}) or not same_site(target,source['url']) or urlsplit(target).path in ('','/'):
                raise ValueError('invalid_catalog_backfill')
            for section in ('incremental_polling','polling'):
                for row in state.get(section,{}).values():
                    if same_site(target,row.get('url','')) and row.get('collector_status') in LIMITED and instant(row['next_check_at'])>clock:
                        raise ValueError('source_rate_limited')
            backfill={**source,'backfill_url':target}
            event=os.getenv('PUBLIC_OBSERVATION','')
            if event:
                if source['platform']!='youtube' or len(event)>4096:raise ValueError('invalid_public_observation')
                backfill['watch_observation']=json.loads(event)
            catalog={**catalog,'sources':[backfill],'permalink_backfill':True}
        else:
            catalog = due_batch(catalog,state,live_platforms(enabled,a.lane),clock)
            catalog['live_lane'] = a.lane
            if a.timer and recent_live_plan(state,clock,a.lane):
                catalog.update(sources=[],live_order=[],news_signals={},collection_scope='recent live plan already acknowledged')
    (root/'sources.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'sources':len(catalog['sources']),'platforms':dict(collections.Counter(s['platform'] for s in catalog['sources']))}))
