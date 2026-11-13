"""Success coverage is separate from admission, attempts and archive inserts."""
from collections import Counter
from datetime import timedelta
from math import ceil
from source_health import instant,EMPTY,LIMITED

UNAVAILABLE={'login_required','http_401','http_403','robots_denied','challenge_required',
             'robots_challenge_required','verified_post_identity_unavailable','unsupported'}


def percentiles(values):
    values=sorted(v for v in values if isinstance(v,(int,float)) and v>=0)
    return {'samples':len(values),**{f'p{p}':round(values[ceil(len(values)*p/100)-1],3) if values else None for p in (50,95,99)},
            'mean':round(sum(values)/len(values),3) if values else None}


def summarize(catalog,state,items,now):
    total=len(catalog.get('sources',[]));active=eligible=checked5=checked15=attempted5=overdue=limited=0
    eligible_checked=0;failed5=0;platforms={};collection=[];delivery=[];sources={};http=Counter()
    for source in catalog.get('sources',[]):
        h=state.get('incremental_polling',{}).get(source['id'],{})
        if h.get('url')!=source['url']:h={}
        enabled=source.get('enabled',True);active+=int(enabled)
        available=enabled and h.get('collector_status') not in UNAVAILABLE
        eligible+=int(available)
        p=platforms.setdefault(source['platform'],Counter())
        p['registered']+=1;p['active']+=int(enabled)
        p['overdue']+=int(enabled and (not h.get('next_check_at') or instant(h['next_check_at'])<=now))
        p['pending_history_sources']+=int(enabled and bool(h.get('remaining_ranges')))
        p['blocked_sources']+=int(enabled and h.get('collector_status') in UNAVAILABLE)
        status=h.get('collector_status','never_checked')
        verified=h.get('last_content_verified_at')
        if verified and instant(verified)<=now and (not p.get('last_archive_verified_at') or instant(verified)>instant(p['last_archive_verified_at'])):
            p['last_archive_verified_at']=verified
        p.setdefault('latest_statuses',Counter())[status]+=1
        if h.get('last_success_at') and (not p.get('last_success_at') or instant(h['last_success_at'])>instant(p['last_success_at'])):
            p['last_success_at']=h['last_success_at']
        events=[r for r in h.get('recent_checks',[]) if enabled and now-timedelta(hours=1)<instant(r['at'])<=now]
        successful=lambda r:r.get('successful',r.get('status') in EMPTY|{'ok'})
        success5=any(successful(r) and now-timedelta(seconds=300)<instant(r['at'])<=now for r in events)
        success15=any(successful(r) and now-timedelta(seconds=900)<instant(r['at'])<=now for r in events)
        # Older receipts may not have the rolling event list.
        if h.get('last_success_at'):
            age=(now-instant(h['last_success_at'])).total_seconds()
            success5|=0<=age<300;success15|=0<=age<900
        attempted=any(now-timedelta(seconds=300)<instant(r['at'])<=now for r in events)
        if h.get('last_checked_at'):
            attempted|=timedelta(0)<=now-instant(h['last_checked_at'])<timedelta(seconds=300)
        failed=any(not successful(r) and now-timedelta(seconds=300)<instant(r['at'])<=now for r in events)
        p['checked_sources_5m']+=int(enabled and attempted)
        p['failed_sources_5m']+=int(enabled and failed)
        p['successful_checks_5m']+=sum(enabled and successful(r) and now-timedelta(seconds=300)<instant(r['at'])<=now for r in events)
        p['failed_checks_5m']+=sum(enabled and not successful(r) and now-timedelta(seconds=300)<instant(r['at'])<=now for r in events)
        p.setdefault('error_categories_1h',Counter()).update(r.get('status','unknown') for r in events if enabled and not successful(r))
        failed5+=int(enabled and any(not successful(r) and now-timedelta(seconds=300)<instant(r['at'])<=now for r in events))
        checked5+=int(enabled and success5);checked15+=int(enabled and success15)
        attempted5+=int(enabled and attempted);eligible_checked+=int(available and success5)
        overdue+=int(enabled and (not h.get('next_check_at') or instant(h['next_check_at'])<=now))
        limited+=int(enabled and h.get('collector_status') in LIMITED)
        p['successful_sources_5m']+=int(enabled and success5)
        p['attempts_1h']+=len(events) if enabled else 0;p['errors_1h']+=sum(not successful(r) for r in events) if enabled else 0
        p['checked_sources_1h']+=int(enabled and bool(events))
        p['successful_sources_1h']+=int(enabled and any(successful(r) for r in events))
        p.setdefault('_delivery',[]).extend(v for e in events for v in e.get('delivery_seconds',[]))
        for event in events:
            if event.get('seconds') is not None:collection.append(event['seconds'])
            delivery.extend(event.get('delivery_seconds',[]))
            for key,value in (event.get('pooled_http') or {}).items():http[key]+=value
        sources[source['id']]={'last_attempt_at':h.get('last_attempt_at') or h.get('last_checked_at'),
            'last_completed_at':h.get('last_completed_at') or h.get('last_checked_at'),
            'last_success_at':h.get('last_success_at'),'next_check_at':h.get('next_check_at'),
            'status':h.get('collector_status','never_checked'),
            'platform':source['platform'],'enabled':enabled,
            'last_new_archived_at':h.get('last_new_post_at'),
            'pending_history_ranges':h.get('remaining_ranges'),
            'consecutive_errors':h.get('consecutive_errors',0),
            'latest_public_post_archived':None,
            'public_comparison_status':'not_measured'}
    new_minute=0;new_windows={'5m':0,'15m':0,'1h':0,'24h':0};last_publication=None
    for item in items:
        if item.get('sample') or not item.get('first_archived_at'):continue
        age=(now-instant(item['first_archived_at'])).total_seconds()
        for label,seconds in (('5m',300),('15m',900),('1h',3600),('24h',86400)):
            new_windows[label]+=int(0<=age<seconds)
        if item.get('published_at') and not item.get('published_at_is_estimated'):
            if last_publication is None or instant(item['published_at'])>instant(last_publication):last_publication=item['published_at']
        p=platforms.setdefault(item['platform'],Counter())
        for label,seconds in (('5m',300),('15m',900),('1h',3600)):
            p['new_posts_'+label]+=int(0<=age<seconds)
        if age>=0 and (not p.get('last_archive_insert_at') or instant(item['first_archived_at'])>instant(p['last_archive_insert_at'])):
            p['last_archive_insert_at']=item['first_archived_at']
        if 0<=age<60:new_minute+=1
    for p in platforms.values():
        for field in ('active','registered','checked_sources_5m','failed_sources_5m',
                      'successful_checks_5m','failed_checks_5m','successful_sources_5m',
                      'overdue','pending_history_sources','blocked_sources',
                      'new_posts_5m','new_posts_15m','new_posts_1h'):
            p.setdefault(field,0)
        p.setdefault('last_archive_insert_at',None)
        p.setdefault('last_archive_verified_at',None)
        p['check_counts_scope']='retained recent_checks; older receipts may lack event counts'
        p['pending_history_scope']='reported remaining_ranges; absence does not prove complete backfill'
        p['collector_availability']='unknown'  # configuration alone does not attest executable/runtime health
        p['error_rate_1h']=p['errors_1h']/p['attempts_1h'] if p['attempts_1h'] else None
        p['delivery_seconds_1h']=percentiles(p.pop('_delivery',[]))
        p['coverage_5m_percent']=100*p['successful_sources_5m']/p['active'] if p['active'] else None
        p['collection_state']=('delivering' if p['new_posts_1h'] else 'checked_no_new_posts' if p['successful_sources_1h']
                               else 'collection_errors' if p['attempts_1h'] else 'not_checked_recently')
    return {'at':now.isoformat(),'registered':total,'active':active,'eligible':eligible,
        'successful_sources_5m':checked5,'successful_sources_15m':checked15,'attempted_sources_5m':attempted5,
        'failed_sources_5m':failed5,'new_posts':new_windows,'last_publication_at':last_publication,
        'coverage_5m_all_percent':100*checked5/total if total else None,
        'coverage_5m_eligible_percent':100*eligible_checked/eligible if eligible else None,
        'eligible_definition':'enabled excluding explicit authentication, robots and unsupported-source blocks; timeouts and rate limits remain included',
        'overdue':overdue,'rate_limited_sources':limited,'new_posts_per_minute':new_minute,
        'collection_seconds_1h':percentiles(collection),'delivery_seconds_1h':percentiles(delivery),
        'pooled_http_1h':dict(http),'pooled_http_requests_per_second_1h':http['requests']/3600 if http else None,
        'http_measurement_scope':'pooled HTTP transport only; excludes browser subresources and external tool traffic',
        'delivery_definition':'first discovery to verified archive readback; not browser render latency',
        'platforms':platforms,'sources':sources}
