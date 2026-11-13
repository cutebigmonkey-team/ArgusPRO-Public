"""Publish collection cursors after durable post import, under the writer lock."""
import argparse
import json
from pathlib import Path
import sys
from drive_store import from_environment
from publish import load_snapshot
sys.path.append(str(Path(__file__).resolve().parents[1]/'collect'))
from cloud_social import require_cloud
from source_health import update
from url_identity import archive_identities


def commit(store,report,*,snapshot=None,return_state=False,identities=None,news_items=None,
           previous_state=None,persist=None,commit_key=None):
    from cursor_journal import clone
    state=clone(previous_state) if previous_state is not None else store.read_json('collection-cursors.json') or {'schema':'argus-collection-cursors-v1','sources':{}}
    items=(snapshot if snapshot is not None else load_snapshot(store))['items']
    archived=archive_identities(items) if identities is None else identities
    for source in report['sources']:
        for key in source.get('canonical_keys',[]):
            if key not in archived: raise ValueError('cursor_before_archive')
    if commit_key and commit_key in state.get('cursor_ack_packages',[]):
        if return_state:return state
        return {'telegram_sources_with_cursor':len(state['sources'])}
    incremental = report.get('incremental') is True
    history=report.get('backfill') is True
    cursors = state.setdefault('history_sources',{}) if history else state.setdefault('incremental_sources', {}) if incremental else state['sources']
    valid={r['source_id']:r for r in report['sources'] if r['platform']=='telegram' or
           ((incremental or history) and r['platform'] in ('rss','mailru','youtube','facebook','instagram','tiktok','threads','vk','ok','x'))}
    for key,value in report.get('state_updates',{}).items():
        if key not in valid or value['url']!=valid[key]['url']: raise ValueError('cursor_source_mismatch')
        if valid[key]['platform']=='facebook':
            from facebook_feed import owned_story_links
            pending = value.get('pending', [])
            if (not isinstance(pending,list) or len(pending)>2048 or
                    set(owned_story_links(valid[key],pending).values())!=set(pending)):
                raise ValueError('invalid_facebook_frontier')
        if value.get('next_url'):
            from browser_public import continuation
            from urllib.parse import urlsplit
            target=value['url']
            if valid[key]['platform']=='vk' and urlsplit(value['next_url']).hostname=='m.vk.com':
                target=urlsplit(target)._replace(netloc='m.vk.com').geturl()
            if continuation(target,value['next_url'])!=value['next_url']:raise ValueError('invalid_browser_continuation')
        before=value.get('before')
        if before is not None and (type(before) is not int or before<1): raise ValueError('invalid_cursor')
        old=cursors.get(key)
        if old and old['updated_at']>value['updated_at']: continue
        if 'based_on' in value and old != value and value['based_on'] != (old or {}).get('updated_at'):
            # A recovered batch advanced this source after planning. Its head
            # and gap list are the basis of the next tick; posts are already safe.
            continue
        cursors[key]=value
    for row in report['sources']:
        if 'checked_at' not in row or 'new_keys' not in row:
            continue
        health = state.setdefault('history_polling' if history else 'incremental_polling' if incremental else 'polling', {})
        key = row['source_id']
        updated = update(health.get(key, {}), row, target_seconds=report.get('poll_target_seconds'),
                         headroom_seconds=report.get('poll_headroom_seconds',0))
        if updated:
            health[key] = updated
            for field in ('last_checked_at','last_success_at','last_new_post_at','next_check_at','collector_status','consecutive_errors',
                          'source_status','collector_name','last_post_id','discovered_posts','archived_posts'):
                row[field]=updated.get(field)
            cursor = cursors.get(key, {})
            if cursor.get('url') == row['url']:
                updated['pagination_cursor'] = cursor.get('before')
    from datetime import datetime,timezone
    from news_order import pending_news
    if incremental and report.get('sources') and not report.get('permalink_backfill'):
        state['last_live_commit_at']=report.get('archive_attempt_at') or datetime.now(timezone.utc).isoformat()
        if report.get('live_planned_at'):
            from source_health import instant
            planned=instant(report['live_planned_at'])
            lane = report.get('live_lane')
            if lane in ('feeds', 'platforms') and planned <= instant(state['last_live_commit_at']):
                plans = state.setdefault('live_plans', {})
                if lane not in plans or planned > instant(plans[lane]):
                    plans[lane] = planned.isoformat()
            previous=state.get('last_live_planned_at')
            if planned<=instant(state['last_live_commit_at']) and (not previous or planned>instant(previous)):
                state['last_live_planned_at']=planned.isoformat()
    as_of=datetime.fromisoformat(report['archive_attempt_at'].replace('Z','+00:00')) if report.get('archive_attempt_at') else datetime.now(timezone.utc)
    state['first_news']=pending_news(items if news_items is None else news_items,state.get('incremental_polling',{}),as_of)
    state['news_snapshot_sha']=(store.read_json('catalog.json') or {}).get('snapshot_sha256')
    state['run_id']=report['run_id']
    if commit_key:state['cursor_ack_packages']=(state.get('cursor_ack_packages',[])+[commit_key])[-8:]
    if persist is not None:verified=persist(state)
    else:
        store.json('collection-cursors.json',state,mutable=True)
        verified=store.read_json('collection-cursors.json')
    if verified!=state and (persist is None or
            {k:v for k,v in verified.items() if k!='cursor_checkpoint'}!={k:v for k,v in state.items() if k!='cursor_checkpoint'}):
        raise ValueError('cursor_readback_mismatch')
    if return_state:return verified
    return {'telegram_sources_with_cursor':len(state['sources']),
            'public_history_exhausted':sum(bool(s.get('exhausted')) for s in state['sources'].values())}


def main():
    require_cloud(); p=argparse.ArgumentParser(); p.add_argument('action',choices=['prepare','commit']); p.add_argument('--path',required=True); args=p.parse_args()
    store=from_environment(); store.assert_private(); path=Path(args.path)
    if args.action=='prepare':
        state=store.read_json('collection-cursors.json') or {'schema':'argus-collection-cursors-v1','sources':{}}
        pointer=store.read_json('catalog.json')
        if 'news_snapshot_sha' not in state or state['news_snapshot_sha']!=(pointer or {}).get('snapshot_sha256'):
            from datetime import datetime,timezone
            from news_order import pending_news
            state['first_news']=pending_news(load_snapshot(store,pointer)['items'],state.get('incremental_polling',{}),datetime.now(timezone.utc))
        path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(state),encoding='utf-8')
        from source_discovery import registered_discoveries
        from site_sources import with_submissions
        catalog = with_submissions(store, store.read_json('sources.json') or {'sources':[]})
        path.with_name('discovered-sources.json').write_bytes(json.dumps({
            'sources':registered_discoveries(catalog)}).encode())
        print(json.dumps({'prepared_cursors':len(state['sources'])}))
    else:
        report=json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps(commit(store,report)))
        path.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')


if __name__=='__main__': main()
