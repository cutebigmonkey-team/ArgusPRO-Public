"""Compact per-source health for the existing private collection monitor."""
from collections import Counter
from collections import defaultdict
from math import ceil
from datetime import timedelta
from source_health import instant, EMPTY
from source_discovery import registered_category


def percentiles(values):
    ordered=sorted(values)
    return {'n':len(ordered),**{f'p{p}':round(ordered[ceil(len(ordered)*p/100)-1],2) if ordered else None
                                for p in (50,90,99)}}


def summarize(catalog,state,items,when):
    days=Counter();hours=Counter();recent={}
    platform_new=Counter()
    fresh_lags=defaultdict(list)
    cutoff=when-timedelta(hours=1)
    per_source=defaultdict(list)
    per_platform=defaultdict(list)
    for post in items:
        sid=post.get('source_id')
        if not sid or post.get('sample'):continue
        if post.get('published_at') and not post.get('published_at_is_estimated'):
            stamp=instant(post['published_at'])
            if sid not in recent or stamp>recent[sid]:recent[sid]=stamp
            seen=post.get('discovered_at') or post.get('observed_at')
            if seen and not post.get('discovery_is_upper_bound') and timedelta(0)<=when-instant(seen)<=timedelta(days=1):
                delay=(instant(seen)-stamp).total_seconds()
                if delay>=0:
                    per_source[sid].append(delay)
                    per_platform[post.get('platform')].append(delay)
        if post.get('first_archived_at'):
            age=when-instant(post['first_archived_at'])
            if timedelta(0)<=age<=timedelta(days=1):
                days[sid]+=1
                if age<=timedelta(hours=1):
                    hours[sid]+=1
                    platform=post.get('platform')
                    platform_new[platform]+=1
                    if (post.get('published_at') and not post.get('published_at_is_estimated')
                            and post.get('discovered_at') and not post.get('discovery_is_upper_bound')):
                        published=instant(post['published_at'])
                        discovered=instant(post['discovered_at'])
                        if cutoff<=published<=discovered<=when:
                            fresh_lags[platform].append((discovered-published).total_seconds())
    window=defaultdict(Counter)
    rows={}; x_rows={}; x_lags=[]
    for source in catalog.get('sources',[]):
        sid=source['id']
        checks=[state.get(k,{}).get(sid,{}) for k in ('incremental_polling','history_polling','polling')]
        checks=[r for r in checks if r.get('url')==source.get('url') and r.get('last_checked_at')]
        last=max(checks,key=lambda r:instant(r['last_checked_at']),default={})
        status=last.get('collector_status')
        if not source.get('enabled',True) or status=='disabled_by_registry':label='DISABLED'
        elif status in ('unsupported','verified_post_identity_unavailable'):label='UNSUPPORTED'
        elif not last:label='NEVER_CHECKED'
        elif last.get('source_status')=='Partial Results':label='PARTIAL'
        elif status and status not in EMPTY|{'ok'}:label='ERROR'
        elif not last.get('next_check_at') or instant(last['next_check_at'])<when:label='DELAYED'
        else:label='ACTIVE'
        platform=source.get('platform')
        totals=window[platform]
        if source.get('enabled', True):totals['enabled_sources']+=1
        attempts=[event for row in checks for event in row.get('recent_checks',[])
                  if cutoff<instant(event['at'])<=when]
        totals['measured_checks']+=len(attempts)
        totals['sources_attempted']+=int(bool(attempts))
        totals['verified_content_checks']+=sum(event['verified'] for event in attempts)
        totals['checks_with_error']+=sum(event['status'] not in EMPTY|{'ok','publisher_only'} for event in attempts)
        verified=[r['last_content_verified_at'] for r in checks if r.get('last_content_verified_at')]
        rows[sid]={'last_content_verified_at':max(verified,key=instant,default=None),'name':source.get('name'),'platform':source.get('platform'),'status':label,'last_result':status,
                   'last_attempt':last.get('last_checked_at'),'next_check_at':last.get('next_check_at'),
                   'actual_interval_seconds':last.get('actual_interval_seconds'),
                   'last_success':max((r['last_success_at'] for r in checks if r.get('last_success_at')),key=instant,default=None),
                   'last_pipeline':last.get('pipeline'), 'remaining_ranges':last.get('remaining_ranges'),
                   'latest_publication':recent[sid].isoformat() if sid in recent else None,'new_1h':hours[sid],'new_24h':days[sid]}
        discovery=next((r['kind'] for r in source.get('references',[])
                        if r.get('kind') in ('discovered_public_feed','discovered_public_channel','reviewed_public_source')),None)
        rows[sid].update(discovery_method=discovery,category=registered_category(source),collector=last.get('collector_name'),
                         discovery_latency_seconds_24h=percentiles(per_source[sid]),
                         successful_checks=sum(r.get('successful_checks',0) for r in checks),
                         failed_checks=sum(r.get('failed_checks',0) for r in checks))
        from source_kind import kind
        rows[sid]['monitoring_kind']=kind(source)
        rows[sid]['monitors_new_publications']=(None if kind(source)=='unresolved_short_link' else kind(source)=='publisher')
        if source.get('platform')=='x':
            engines={}
            for check in sorted(checks,key=lambda r:instant(r['last_checked_at'])):
                for name,engine in check.get('engines',{}).items():
                    if engine.get('last_checked_at') and (name not in engines or instant(engine['last_checked_at'])>instant(engines[name]['last_checked_at'])):
                        engines[name]=engine
            x_rows[sid]={**rows[sid], 'url':source['url'], 'enabled':source.get('enabled',True),
                         'last_poll':last.get('x_pipeline'), 'collectors':engines,
                         'remaining_ranges':last.get('remaining_ranges')}
    for post in items:
        if (post.get('platform')!='x' or post.get('sample') or post.get('published_at_is_estimated')
                or post.get('discovery_is_upper_bound') or not post.get('published_at') or not post.get('discovered_at')):
            continue
        detected=instant(post['discovered_at'])
        lag=(detected-instant(post['published_at'])).total_seconds()
        if when-timedelta(days=1)<=detected<=when and lag>=0:
            x_lags.append(lag)
    x_lags.sort()
    pipeline=[r['last_poll'] for r in x_rows.values() if r['last_poll']]
    skips=Counter()
    for poll in pipeline:
        skips.update(poll['skipped_by_reason'])
    x={'configured':len(x_rows),'enabled':sum(r['enabled'] for r in x_rows.values()),
       'ever_checked':sum(bool(r['last_attempt']) for r in x_rows.values()),
       'checked_24h':sum(bool(r['last_attempt']) and when-timedelta(days=1)<=instant(r['last_attempt'])<=when for r in x_rows.values()),
       'last_success':max((r['last_success'] for r in x_rows.values() if r['last_success']),key=instant,default=None),
       'latest_poll_totals':{'measured_sources':len(pipeline),
           **{k:sum(p[k] for p in pipeline if p.get(k) is not None) if any(p.get(k) is not None for p in pipeline) else None for k in ('found','processed','saved_new','already_archived','verified')},
           'skipped':sum(skips.values()), 'skipped_by_reason':dict(skips)},
       'detection_seconds_24h':{'samples':len(x_lags), **{'p'+str(p):x_lags[ceil(len(x_lags)*p/100)-1] if x_lags else None for p in (50,90,99)}},
       'sources':x_rows}
    platforms={}
    for row in rows.values():
        p=platforms.setdefault(row['platform'],Counter())
        p['configured']+=1
        p['registered_from_discovery']+=int(bool(row['discovery_method']))
        p['active_sources']+=int(row['status']=='ACTIVE')
        p['overdue_sources']+=int(row['status']=='DELAYED')
        p['successful_checks']+=row['successful_checks']
        p['failed_checks']+=row['failed_checks']
        p['sources_archiving_24h']+=int(row['new_24h']>0)
        p['new_24h']+=row['new_24h']
        p['pending_ranges']+=row['remaining_ranges'] or 0
    hourly={platform:{**counts,'new_archive_ids':platform_new[platform],'fresh_dated_ids':len(fresh_lags[platform]),
                      'detection_seconds':percentiles(fresh_lags[platform])} for platform,counts in window.items()}
    return {'platforms_last_hour':hourly,'x':x,'as_of':when.isoformat(),'registered':len(rows),'counts':dict(Counter(r['status'] for r in rows.values())),
            'platforms':platforms,'discovery_latency_seconds_24h':{p:percentiles(per_platform[p]) for p in platforms},'sources':rows}
