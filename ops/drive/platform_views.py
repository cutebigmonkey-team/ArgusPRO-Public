"""Readable per-platform Drive views, rebuilt from the deduplicated catalog."""
from private_assets import private_asset_path
import argparse
import hashlib
import json
import os
from pathlib import Path
from drive_store import DriveStore, from_environment
from publish import load_snapshot, now

def render_posts(items):
    chunks=['ARGUS — зібрані публікації\nМедіа в цьому запуску збережено як посилання на оригінали.\n']
    for item in items:
        chunks.append('\n'.join([item.get('published_at') or 'Дата не вказана',item.get('source_name',''),
            'Публічна картка: текст може бути скорочений платформою.' if item.get('text_may_be_truncated') else '',
            item['url'],item['text'],*item.get('media_urls',[]),'\n'+'─'*50]))
    return '\n\n'.join(chunks).encode('utf-8')

def newer_run(previous,report):
    old=str((previous or {}).get('run_id',''))
    new=str(report.get('run_id',''))
    return old.isdigit() and new.isdigit() and int(old)>int(new)

def write_views(stores, items, report):
    output={}
    for platform, store in stores.items():
        if report.get('platform_scope') and platform not in report['platform_scope']:continue
        store.assert_private()
        posts=sorted((p for p in items if p['platform']==platform and not p.get('sample')),
                     key=lambda p:(p.get('published_at') or '',p['id']),reverse=True)
        summary=next((p for p in report['platforms'] if p['platform']==platform),{'platform':platform,'status':'not_run'})
        records=[s for s in report['sources'] if s['platform']==platform]
        store.json('posts.json',{'posts':posts},mutable=True)
        store.put('posts.txt',render_posts(posts),mutable=True)
        output[platform]=len(posts)
        if newer_run(store.read_json('collection-report.json'),report):continue
        store.json('collection-report.json',{'updated_at':report['updated_at'],'run_id':report['run_id'],
                   'summary':summary,'sources':records},mutable=True)
        text=['ARGUS — результат збору',report['updated_at'],f'Публікацій у папці: {len(posts)}',
              f"Статус: {summary.get('status')}",f"Відкладених джерел: {summary.get('deferred',0)}",'']
        text += [f"{r['name']} — {r['status']} — постів: {r['posts']}\n{r['url']}" for r in records]
        if summary.get('status')=='no_sources': text.append('У доступному каталозі немає адрес для цієї платформи.')
        store.put('status.txt','\n'.join(text).encode('utf-8'),mutable=True)
    return output

def merge_catalog(previous,current):
    scope=current.get('platform_scope')
    if not scope:
        # A full run can finish after a newer incremental writer registered
        # reviewed profiles. Preserve those independently maintained sources.
        from import_sheet import preserve_verified_profiles
        from source_discovery import add_registered
        return add_registered(preserve_verified_profiles(current,previous),previous) if previous else current
    # A focused collection updates its own source list, never removes platforms
    # whose collectors did not participate in this run.
    merged={**(previous or current)}
    merged['sources']=[s for s in (previous or {}).get('sources',[]) if s['platform'] not in scope]+current['sources']
    merged.pop('platform_scope',None)
    merged['collection_scope']='all supplied platform sources'
    from source_discovery import add_registered
    return add_registered(merged,previous)

def monitor_stages(previous,report):
    stages=[{'tool':p['platform'],'status':p['status'],'observed_at':report['updated_at'],
             'posts':p['posts'],'results':p['results'],'metrics':[]} for p in report['platforms']]
    if report.get('platform_scope'):
        stages=[s for s in (previous or {}).get('stages',[]) if s['tool'] not in report['platform_scope']]+stages
    return stages

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('Publication requires GitHub-hosted Actions')
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True);args=parser.parse_args()
    root=from_environment();root.assert_private()
    directory=Path(args.input);report=json.loads((directory/'report.json').read_text(encoding='utf-8'))
    mapping=json.loads(private_asset_path('platform-folders.json').read_text(encoding='utf-8'))
    if mapping['root']!=root.root: raise ValueError('Root mismatch')
    stores={}
    for key,folder in mapping['platforms'].items():
        if report.get('platform_scope') and key not in report['platform_scope']:continue
        info=root.service.files().get(fileId=folder['folder_id'],fields='parents,name').execute(num_retries=3)
        if root.root not in info.get('parents',[]) or info.get('name')!=folder['name']: raise ValueError('Unexpected platform folder')
        stores[key]=DriveStore(root.service,folder['folder_id'])
    items=load_snapshot(root)['items']
    # The verified collection receipt is independent of the derived text/JSON
    # views. Keep it readable even if a platform folder upload times out.
    root.json('collection-report-'+report['run_id']+'.json',report,mutable=True)
    previous=root.read_json('monitor.json') or {}
    if not newer_run(previous,report):
        catalog=json.loads((directory/'sources.json').read_text(encoding='utf-8'))
        root.json('sources.json',merge_catalog(root.read_json('sources.json'),catalog),mutable=True)
    counts=write_views(stores,items,report)
    if newer_run(previous,report):
        print(json.dumps({'private_platform_post_counts':counts,'newer_monitor_retained':True}))
        return
    root.json('monitor.json',{**previous,'format':'argus-monitor-v1','updated_at':now(),'run_id':report['run_id'],
        'repository':'cutebigmonkey-team/ArgusPRO','state':'partial' if any(
            k not in ('ok','no_public_posts','no_new_posts','not_due','disabled_by_registry')
            for p in report['platforms'] for k in p.get('results',{})) else 'passed',
        'stages':monitor_stages(previous,report)},mutable=True)
    print(json.dumps({'private_platform_post_counts':counts}))

if __name__=='__main__':
    try:
        import time
        for attempt in range(3):
            try:
                main()
                break
            except (TimeoutError,ConnectionError):
                if attempt==2:raise
                time.sleep(2**attempt)
    except Exception as error:
        print('Platform view publication failed:',type(error).__name__)
        raise SystemExit(1)
