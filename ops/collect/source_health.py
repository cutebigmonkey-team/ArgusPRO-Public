"""Source polling and reader preferences saved in the existing collection checkpoint."""
from contextvars import ContextVar
from datetime import datetime, timedelta


reader_history = ContextVar('reader_history', default={})
retry_hints = ContextVar('retry_hints',default=None)
SKIPPED = {'disabled_by_registry', 'deferred_rate_limit', 'not_due', 'deferred_budget'}
LIMITED = {'http_429', 'rate_limited', 'gateway_rate_limited'}
BLOCKED = {'login_required', 'http_401', 'http_403', 'challenge_required', 'robots_denied'}
EMPTY = {'no_public_posts', 'no_new_posts'}


def effective_status(record):
    status=record['status']
    if status in EMPTY|{'ok'} and (record.get('fetch_complete') is False or
            (record.get('remaining_ranges') and not record.get('canonical_keys'))):
        return 'partial_results'
    return status


def platform_status(configured, statuses, posts):
    if not configured:
        return 'no_sources'
    statuses = set(statuses)
    attempted = statuses - SKIPPED
    if not attempted:
        return 'disabled' if statuses <= {'disabled_by_registry'} else 'partial'
    if statuses.intersection(SKIPPED - {'disabled_by_registry'}):
        return 'partial'
    if 'partial_results' in attempted:
        return 'partial'
    if attempted - EMPTY - {'ok'}:
        return 'partial' if posts or attempted.intersection(EMPTY | {'ok'}) else 'failed'
    return 'collected' if posts else 'no_posts'


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def note_retry_after(value,when):
    from email.utils import parsedate_to_datetime
    hints=retry_hints.get()
    if hints is None or not value:return
    try:
        until=when+timedelta(seconds=int(value)) if str(value).isdigit() else parsedate_to_datetime(value)
        if until>when:hints.append(until.isoformat())
    except (ValueError,TypeError,OverflowError):pass


def preferred(tools):
    # Ties preserve the installed chain. Untried readers get a neutral prior.
    stats = reader_history.get()
    return sorted(tools, key=lambda tool: -stats.get(tool, {}).get('score', 0))


def update(previous, record, *, target_seconds=None, headroom_seconds=0):
    if target_seconds is not None and (type(target_seconds) is not int or not 60 <= target_seconds <= 86400):
        raise ValueError('invalid_poll_target')
    if type(headroom_seconds) is not int or not 0<=headroom_seconds<=30:raise ValueError('invalid_poll_headroom')
    when = record['checked_at']
    if previous.get('url') != record['url']:
        previous = {}
    if previous.get('last_checked_at') and instant(previous['last_checked_at']) >= instant(when):
        return previous
    status = effective_status(record)
    if status in SKIPPED:
        return previous
    checked = instant(when)
    new = set(record['new_keys'])
    success = status == 'ok' or status in EMPTY or (status=='publisher_only' and bool(record.get('canonical_keys')))
    failures = 0 if success else previous.get('consecutive_errors', 0) + 1
    quiet = previous.get('quiet_checks', 0) + 1 if success and not new else 0
    platform = record.get('platform')
    base = 1 if platform in ('telegram','rss') else 10 if platform in ('vk','ok','max') else 5
    interval = min(base * 2**min(quiet,3), base * 5)
    # A quiet poll immediately after a publication is common for a live publisher.
    # Keep it eligible for the next tick while its confirmed publication is recent.
    if success and any(stamp and timedelta(0) <= checked - instant(stamp) <= timedelta(minutes=30)
                       for stamp in (record.get('latest_published_at'), previous.get('last_post_published_at'))):
        interval = min(interval, 1)
    # Logged-out walls can expose stable post IDs without a publication date.
    # Keep the existing platform base interval for a verified populated wall.
    if success and platform in ('vk','ok') and record.get('delivery',{}).get('saved_ids'):
        interval = min(interval, base)
    if success and target_seconds is not None:
        # The scheduler target is a ceiling, not a replacement for adaptive
        # freshness. Preserve Telegram's existing cloud interval.
        interval = ((target_seconds-headroom_seconds) / 60 if platform=='telegram'
                    else min(interval, (target_seconds-headroom_seconds) / 60))
    if failures:
        interval = min(7*24*60, max(15, base) * 2**min(failures,10))
    if status in ('timeout','network_error','dns_error','http_502','http_503','http_504'):
        interval = min(interval, 60)
    if platform == 'x' and status in {'connection_reset','http_500','stale_feed_only','x_incomplete_response'}:
        interval = min(interval, 60)
    if platform == 'x' and success and record.get('remaining_ranges'):
        interval = 1
    if status in LIMITED | BLOCKED:
        interval = max(interval, 24*60 if status in BLOCKED else 60)
    if status == 'stale_feed_only':
        # Reachable public feed; retry for a fresh head without escalating to a week.
        interval = min(interval, 60)
    if failures:
        import hashlib
        # Stable for replay of one observation; spread retries across the host.
        fraction=int.from_bytes(hashlib.sha256((record['url']+when).encode()).digest()[:2],'big') / 65535
        interval=min(7*24*60,interval*(1+0.1*fraction))
    if status=='partial_results' and record.get('remaining_ranges'):
        # A retained continuation is unfinished collection, not source silence.
        # Server retry deadlines below remain authoritative.
        interval=1
    if (platform=='youtube' and status=='partial_results' and record.get('remaining_ranges')
            and any(a.get('tool')=='youtube_uploads' and a.get('status')=='ok' for a in record['attempts'])):
        # A verified playlist with deferred details is unfinished work, not an
        # origin outage. Do not strand its durable queue in exponential backoff.
        interval=1
    next_check=checked+timedelta(minutes=interval)
    host_until=None
    if record.get('retry_after'):
        next_check=max(next_check,instant(record['retry_after']))
    if status in LIMITED:
        host_until=checked+timedelta(hours=1)
        if record.get('retry_after'):
            host_until=max(host_until,instant(record['retry_after']))
            next_check=max(next_check,host_until)
    if platform=='x' and status in LIMITED:
        # A reset time applies to this shared quota. Repeated failures should
        # not leave a source asleep for days after the quota is available.
        host_until=max(checked+timedelta(minutes=1),instant(record['retry_after'])) if record.get('retry_after') else checked+timedelta(hours=1)
        next_check=host_until
    elif status=='http_503' and record.get('retry_after'):
        host_until=instant(record['retry_after'])
    engines = dict(previous.get('engines', {}))
    for attempt in record['attempts']:
        tool = attempt.get('tool')
        if not tool:
            continue
        verified = attempt.get('keys', [])
        fresh = len(new.intersection(verified))
        ok = attempt['status'] == 'ok' or attempt['status'] in EMPTY
        prior = engines.get(tool, {})
        # Wall time estimates runtime cost. Publication lag is available only for dated posts.
        seconds = attempt.get('seconds', attempt.get('wall_seconds',record['seconds'] / max(1, len(record['attempts']))))
        lag = attempt.get('publication_lag_hours')
        score = (int(ok) + min(4, fresh) + attempt.get('full', 0)/max(1,len(verified))
                 + attempt.get('dated', 0)/max(1,len(verified)) - 2*int(not ok)
                 - 2*int(attempt['status'] in LIMITED) - min(seconds/60, 2))
        if fresh and lag is not None:
            score += 1 / (1 + lag/24)
        score -= min(1,attempt.get('max_process_rss_kib',0)/(512*1024))
        engines[tool] = {'runs':prior.get('runs',0)+1, 'successes':prior.get('successes',0)+int(ok),
                         'errors':prior.get('errors',0)+int(not ok), 'new_posts':prior.get('new_posts',0)+fresh,
                         'last_status':attempt['status'], 'last_error_type':attempt.get('error_type'), 'last_checked_at':when, 'last_seconds':round(seconds,2), 'max_process_rss_kib':attempt.get('max_process_rss_kib'),
                         'last_publication_lag_hours':lag,
                         'score':round(prior.get('score',score)*0.6 + score*0.4, 3)}
    if status in LIMITED:
        label = 'Rate Limited'
    elif status in ('login_required','http_401'):
        label = 'Login Required'
    elif status in ('stale_feed_only','publisher_only','partial_results') or record.get('remaining_ranges') or any(
            surface.get('stop') in {'time_budget','snapshot_limit','login_prompt','login_required','http_429'}
            for attempt in record['attempts'] for surface in attempt.get('surfaces',[]) if record.get('posts')):
        label = 'Partial Results'
    elif success:
        label = 'Healthy' if new else 'No New Posts'
    elif record.get('canonical_keys'):
        label = 'Partial Results'
    elif status in BLOCKED or status.startswith(('http_', 'robots_', 'tls_')) or status in ('timeout','dns_error','challenge_required'):
        label = 'Source Unavailable'
    else:
        label = 'Parsing Error'
    latest_date = record.get('latest_published_at')
    last_id = previous.get('last_post_id')
    if latest_date and (not previous.get('last_post_published_at') or
                        instant(latest_date) >= instant(previous['last_post_published_at'])):
        last_id = record.get('latest_post_id') or last_id
    else:
        latest_date = previous.get('last_post_published_at')
    # Private rolling receipts distinguish an attempted check from a confirmed
    # content read. Store only time, outcome and verification; archive deltas own the IDs.
    recent_checks = [event for event in previous.get('recent_checks', [])
                     if checked-timedelta(hours=1) < instant(event['at']) <= checked]
    recent_checks.append({'at':when,'status':status,'verified':bool(record.get('delivery',{}).get('saved_ids')),
                          'successful':success,'new_posts':len(new),'seconds':record.get('seconds'),
                          'pooled_http':record.get('pooled_http'),
                          'delivery_seconds':(record.get('collection_latency') or {}).get('discovery_to_archive_readback',[])})
    return {'url':record['url'], 'last_checked_at':when, 'recent_checks':recent_checks,
            'actual_interval_seconds':(checked-instant(previous['last_checked_at'])).total_seconds() if previous.get('last_checked_at') else None,
            'last_attempt_at':record.get('started_at') or when,'last_completed_at':when,
            'consecutive_failures':failures,
            'last_success_at':when if success else previous.get('last_success_at'),
            'last_extraction_at':when if (record.get('collector_rows') or 0)>0 else previous.get('last_extraction_at'),
            'successful_checks':previous.get('successful_checks',0)+int(success),
            'failed_checks':previous.get('failed_checks',0)+int(not success),
            'last_new_post_at':when if new else previous.get('last_new_post_at'),
            'next_check_at':next_check.isoformat(), 'host_cooldown_until':host_until.isoformat() if host_until else None,'quiet_checks':quiet,
            'source_status':label, 'collector_name':next((a['tool'] for a in record['attempts']
                if a.get('tool') and a.get('posts')),next((a['tool'] for a in record['attempts'] if a.get('tool')),None)),
            'discovered_posts':len(new),'archived_posts':len(set(record.get('canonical_keys',[]))),
            'last_post_id':last_id, 'last_post_published_at':latest_date,
            'remaining_ranges':record.get('remaining_ranges'),
            **({'x_pipeline':{'found':record.get('parser_inputs'), 'processed':record.get('collector_rows'),
                              'saved_new':len(new), 'already_archived':len(set(record.get('canonical_keys',[]))-new), 'verified':len(set(record.get('canonical_keys',[]))),
                              'skipped_by_reason':record.get('parser_exclusions',{}),
                              'unavailable_ranges':[a['unavailable_range'] for a in record['attempts'] if a.get('unavailable_range')]}} if platform=='x' else {}),
            'delivery_target_seconds':target_seconds or (300 if platform in ('telegram','rss') else 1800),
            'collection_latency':record.get('collection_latency'),
            'last_content_verified_at':when if record.get('delivery',{}).get('saved_ids') else previous.get('last_content_verified_at'),
            'delivery':record.get('delivery'),
            'pipeline':{'collector_rows':record.get('collector_rows'),
                        'collection_seconds':record.get('seconds'),
                        'parser_inputs':record.get('parser_inputs'),
                        'rejected_records':len(record['rejections']) if isinstance(record.get('rejections'),list) else None,
                        'gateway':[{k:a[k] for k in ('status','seconds','posts','gateway_admitted','gateway_request_seconds','gateway_retry_seconds') if k in a}
                                   for a in record['attempts'] if a.get('tool')=='rss_bridge'],
                        'parser_exclusions':record.get('parser_exclusions'),
                        'reported_pages':sum(len(a['pages']) for a in record['attempts'] if isinstance(a.get('pages'),list)),
                        'unique_keys':len(set(record.get('canonical_keys',[]))),
                        'new_keys':len(new),
                        'archive_verified_keys':len(set(record.get('canonical_keys',[])))},

            'collector_status':('no_new_posts' if status=='ok' and not new else status),
            'consecutive_errors':failures, 'engines':engines}
