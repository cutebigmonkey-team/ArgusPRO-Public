"""Read-only source audit. Requires existing authorized DB/Drive credentials.

No scheduler construction, leases, advisory locks, DDL, collectors or writes.
Archive totals are taken from the published receipt at its stated timestamp;
missing or stale receipts are unknown, never zero. Output is private evidence.
"""
import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import os
from pathlib import Path
import re
import sys

OPS=Path(__file__).resolve().parents[1]
for directory in ('collect','drive','local-storage','hostless'):
    sys.path.insert(0,str(OPS/directory))
from monitoring_metrics import summarize


def audit(catalog, state, monitor, schedule, pending, now):
    rows={r['id']:r for r in schedule}
    effective=[];polling={};sources=[]
    pending_by_id={r['source_id']:r for r in pending}
    for source in catalog.get('sources',[]):
        sid=source['id'];r=rows.get(sid);h=json.loads(r['health']) if r else {}
        if h.get('url')!=source['url']:h={}
        polling[sid]=h
        effective.append({**source,'enabled':bool(r['enabled']) if r else False})
        sources.append({'source_id':sid,'platform':source['platform'],'url':source['url'],
            'configured_enabled':source.get('enabled',True),'runtime_enabled':bool(r['enabled']) if r else None,
            'last_success_at':h.get('last_success_at'),'last_new_archived_at':h.get('last_new_post_at'),
            'next_check_epoch':r['next_check_at'] if r else None,
            'last_status':h.get('collector_status'),'consecutive_errors':r['failures'] if r else None,
            'lease_until_epoch':r['lease_until'] if r else None,
            'pending_result':pending_by_id.get(sid),
            'checkpoint':state.get('sources',{}).get(sid),
            'history_checkpoint':state.get('history_sources',{}).get(sid),
            'unfinished_ranges':h.get('remaining_ranges'),
            'latest_public_post_archived':None,'public_comparison':'not_measured'})
    coverage=summarize({'sources':effective},{'incremental_polling':polling},[],now)
    receipt=monitor.get('coverage',{});stamp=receipt.get('at')
    age=(now-datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds() if stamp else None
    fresh=age is not None and 0<=age<300
    table=[]
    for platform,p in coverage['platforms'].items():
        published=receipt.get('platforms',{}).get(platform,{})
        missing=sum(s['platform']==platform and s['id'] not in rows for s in catalog['sources'])
        table.append({'platform':platform,'configured_sources':sum(s['platform']==platform for s in catalog['sources']),
            'configured_enabled_sources':sum(s['platform']==platform and s.get('enabled',True) for s in catalog['sources']),
            'sources_without_runtime_row':missing,
            'runtime_enabled_sources':p['active'] if not missing else None,'checked_sources_5m':p['checked_sources_5m'],
            'successful_checks_5m':p['successful_checks_5m'],'failed_checks_5m':p['failed_checks_5m'],
            'overdue':p['overdue'],'pending_history_sources':p['pending_history_sources'],
            'error_categories_1h':dict(p.get('error_categories_1h',{})),
            'new_archived_posts':{w:published.get('new_posts_'+w) if fresh else None for w in ('5m','15m','1h')},
            'archive_metrics_at':stamp,'archive_receipt_fresh':fresh,
            'last_archive_verified_at':p.get('last_archive_verified_at'),
            'collector_availability':'unknown'})
    return {'observed_at':now.isoformat(),'observed_at_kyiv':now.astimezone(ZoneInfo('Europe/Kyiv')).isoformat(),
            'mode':'read_only','archive_receipt_age_seconds':age,
            'deployment_sha_reported':monitor.get('runtime',{}).get('deployment_sha'),
            'deployment_identity_scope':'published monitor; not provider active-deployment proof',
            'platforms':table,'sources':sources,
            'limitations':['No latest-public-post comparison or frontend rendering attestation.',
                           'Archive windows use the receipt timestamp; stale/missing values are null.',
                           'Provider readiness, restarts and OOM events require provider diagnostics.']}


NOT_MEASURABLE='NOT_MEASURABLE'


def operational_report(catalog,schedule,pending,monitor,now,packages=None):
    """Latest acknowledged source checks, not invented window totals.

    Payloads, cursors, URLs, raw errors and environment values are excluded.
    A deployed SHA from monitor is reported, never provider-attested.
    """
    from collector_preflight import instagram_gateway_candidate,incremental_route
    from source_kind import video_scope
    def measurable(value):return value if value is not None else NOT_MEASURABLE
    def instant(value):
        try:
            if isinstance(value,(int,float)):return datetime.fromtimestamp(value,timezone.utc)
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            return parsed if parsed.tzinfo is not None else None
        except (ValueError,TypeError,AttributeError,OverflowError):return None
    def latest(values):
        stamps=[t for v in values if (t:=instant(v)) is not None and t<=now]
        return max(stamps).isoformat() if stamps else None
    def total(values):
        values=list(values)
        return sum(values) if values and all(type(v) is int and v>=0 for v in values) else None
    rows={r['id']:r for r in schedule};platforms=[];scopes=[];source_metrics=[]
    for platform in sorted({s['platform'] for s in catalog.get('sources',[])}):
        sources=[s for s in catalog['sources'] if s['platform']==platform]
        health=[]
        for source in sources:
            row=rows.get(source['id'])
            try:h=json.loads(row['health']) if row else {}
            except (ValueError,TypeError):h={}
            if h.get('url')!=source['url']:h={}
            # Source attempt timestamps are updated on durable staging before
            # archive acknowledgement; health may still describe the prior check.
            staged_attempt=row.get('last_attempt_at') if row else None
            h=dict(h)
            h['_observed_attempt']=latest([h.get('last_attempt_at'),staged_attempt if staged_attempt and staged_attempt>0 else None])
            health.append(h)
            pipeline=h.get('pipeline') or {}
            source_metrics.append({'source_id':source['id'],'platform':platform,
                'actual_interval_seconds':measurable(h.get('actual_interval_seconds')),
                'last_attempt':measurable(latest([h.get('_observed_attempt')])),
                'last_successful_extraction':measurable(latest([h.get('last_extraction_at')])),
                'check_seconds':measurable(pipeline.get('collection_seconds')),
                'extracted_posts':measurable(pipeline.get('collector_rows')),
                'accepted_posts':measurable(pipeline.get('unique_keys')),
                'rejected_posts':measurable(pipeline.get('rejected_records')),
                'gateway':pipeline.get('gateway',NOT_MEASURABLE)})
            if platform in {'rutube','youtube','tiktok'}:
                scopes.append({'source_id':source['id'],'platform':platform,'scope':video_scope(source),
                               'correction':'review_publisher_url' if video_scope(source) in {'video','livestream','unresolved_short_link'} else 'none',
                               'automatic_registry_change':False})
        times=[instant(p.get('created_at')) for p in pending if any(s['id']==p.get('source_id') for s in sources)]
        known=[t for t in times if t is not None and t<=now]
        pipes=[h.get('pipeline') or {} for h in health]
        errors={}
        # Fixed status categories only: never export upstream exception strings.
        allowed={'timeout','http_429','http_403','http_401','login_required','challenge_required',
                 'unsupported','tool_unavailable','robots_denied','gateway_rate_limited',
                 'gateway_busy','gateway_cooldown','parsing_error','extraction_failed','collection_failed'}
        for h in health:
            status=h.get('collector_status')
            if status in allowed:errors[status]=errors.get(status,0)+1
        platforms.append({'platform':platform,'configured_sources':len(sources),
            'scheduled_sources':sum(bool(rows[s['id']]['enabled']) for s in sources) if all(s['id'] in rows for s in sources) else NOT_MEASURABLE,
            'last_attempt':measurable(latest(h.get('_observed_attempt') for h in health)),
            'last_successful_extraction':measurable(latest(h.get('last_extraction_at') for h in health)),
            'extracted_post_count':measurable(total(p.get('collector_rows') for p in pipes)),
            'accepted_post_count':measurable(total(p.get('unique_keys') for p in pipes)),
            'rejected_post_count':measurable(total(p.get('rejected_records') for p in pipes)),
            'archived_post_count':measurable(total((h.get('delivery') or {}).get('saved_ids') for h in health)),
            'frontend_visible_post_count':NOT_MEASURABLE,'error_reasons':errors or NOT_MEASURABLE,
            'oldest_pending_result_seconds':max(0,(now-min(known)).total_seconds()) if len(known)==len(times) and known else (0 if not times else NOT_MEASURABLE),
            'instagram_gateway_candidate_sources':sum(instagram_gateway_candidate(s) for s in sources),
            'route':incremental_route(sources[0]),
            'counts_scope':'sum of latest acknowledged check per source; not unique archive totals or time-window totals'})
    package_times=[instant(p.get('staged_at')) for p in packages] if packages is not None else []
    valid=[t for t in package_times if t is not None and t<=now]
    sha=monitor.get('runtime',{}).get('deployment_sha')
    if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{40}',sha):sha=NOT_MEASURABLE
    return {'platforms':platforms,'source_metrics':source_metrics,'source_scope_review':scopes,'current_deployed_commit':NOT_MEASURABLE,
            'monitor_reported_commit':sha,'deployment_identity_scope':'monitor receipt only; provider active image not verified',
            'source_health_scope':'last acknowledged checks; last_attempt also includes durably staged checks, not in-flight requests',
            'pending_package_count':len(packages) if packages is not None else NOT_MEASURABLE,
            'oldest_pending_package_seconds':max(0,(now-min(valid)).total_seconds()) if valid and len(valid)==len(package_times) else (0 if packages==[] else NOT_MEASURABLE)}


def write_private_report(path,result):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as out:json.dump(result,out,ensure_ascii=False,indent=2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'argus_drive_[a-f0-9]{24}',args.schema):parser.error('Invalid existing schema')
    from drive_store import from_environment
    from registry import fetch_registry
    from worker import merge_catalog
    from site_sources import with_submissions
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    store=from_environment();store.assert_private()
    shipped=json.loads((OPS/'collect/sources.json').read_text())
    remote=with_submissions(store,store.read_json('sources.json') or {'sources':[]})
    catalog=merge_catalog(shipped,remote)
    from cursor_journal import CursorJournal
    state=CursorJournal(store).state
    monitor=store.read_json('monitor.json') or {}
    registry=fetch_registry(os.environ['ARGUS_JSONBIN_BIN_ID'],os.environ['ARGUS_JSONBIN_KEY'],
                            os.getenv('ARGUS_JSONBIN_KEY_TYPE','access'))
    # Use the existing writer's connection settings, with a fresh read-only
    # session. Never instantiate PostgresJournal or acquire its writer lock.
    with psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row,
                         options='-c default_transaction_read_only=on -c statement_timeout=15000') as db:
        db.execute('SET TRANSACTION READ ONLY')
        schedule=db.execute(sql.SQL('SELECT id,source,health,next_check_at,enabled,failures,lease_until,last_attempt_at FROM {}.runtime_schedule').format(sql.Identifier(args.schema))).fetchall()
        pending=db.execute(sql.SQL('SELECT source_id,bytes,created_at FROM {}.runtime_results').format(sql.Identifier(args.schema))).fetchall()
        exists=db.execute('SELECT to_regclass(%s) AS name',(args.schema+'.runtime_pending',)).fetchone()
        packages=(db.execute(sql.SQL("SELECT report::jsonb->>'pending_staged_at' AS staged_at FROM {}.runtime_pending").format(sql.Identifier(args.schema))).fetchall() if exists['name'] else None)
    now=datetime.now(timezone.utc)
    result=audit(catalog,state,monitor,schedule,pending,now)
    result['operational']=operational_report(catalog,schedule,pending,monitor,now,packages)
    from collector_preflight import route_inventory
    result['routes']=route_inventory(catalog)
    result['deployment_sha_reported']=result['operational']['monitor_reported_commit']
    # Existing private audit included cursor payloads. Diagnostic exports must
    # not leak cursor/session tokens, credentials or token-bearing source URLs.
    for source in result['sources']:
        for key in ('url','checkpoint','history_checkpoint','last_status'):source.pop(key,None)
    result['collector_registry']=[{'platform':r['platform'],'enabled':r['enabled'],'engine':r['engine']} for r in registry['collectors']]
    write_private_report(args.output,result)
    print(json.dumps({'output':str(args.output),'sources':len(result['sources']),'production_modified':False}))


if __name__=='__main__':
    try:main()
    except Exception:
        # No raw SDK/SQL exception or traceback: they may contain credentials.
        print(json.dumps({'error':'diagnostic_read_failed','production_modified':False}),file=sys.stderr)
        raise SystemExit(1)
