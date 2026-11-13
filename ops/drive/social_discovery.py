"""Separate, durable discovery index. Never downgrade captured posts to links."""
from private_assets import private_asset_path
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import sys
from drive_store import from_environment,encoded,MAX_JSON,DriveStore
from publish import load_snapshot
sys.path.append(str(Path(__file__).resolve().parents[1]/'collect'))
from social_links import identity,merge_records
from cloud_social import require_cloud
from source_discovery import merge_candidates, apply_reviews, enroll, moderation_queue, registered_discoveries, source_counts


def load_state(store):
    pointer=store.read_json('discovery-catalog.json')
    if not pointer: return {'records':[],'baselines':{},'report':{}}
    state=store.read_json(pointer['snapshot'])
    if hashlib.sha256(encoded(state)).hexdigest()!=pointer['sha256']: raise ValueError('discovery_integrity_mismatch')
    return state


def render(records):
    lines=['ARGUS — знайдені посилання на публікації',
           'Текст оригінальних дописів не отримано. Пошуковий уривок не є повним текстом.\n']
    for row in records:
        lines += [row['url'],'Статус: '+row['status']]
        if row.get('snippet'): lines += ['Пошуковий уривок: '+row['snippet']]
        for evidence in row['evidence']:
            lines += ['Знайдено через '+evidence['method']+': '+(evidence.get('source_url') or evidence.get('query',''))]
        lines.append('')
    return '\n'.join(lines).encode('utf-8')


def publish_discovery(store,result,disabled=()):
    store.assert_private(); previous=load_state(store)
    for row in result['records']:
        target=identity(row['url'])
        if not target or target['canonical_key']!=row['canonical_key'] or target['platform']!=row['platform']:
            raise ValueError('invalid_discovery_identity')
        if row.get('text') or row.get('content_status')!='not_fetched' or row['status'] not in ('link_only','search_snippet'):
            raise ValueError('discovery_must_not_claim_full_content')
    records=merge_records(previous['records'],result['records'])
    baselines=dict(previous['baselines'])
    for url,snapshot in result['baselines'].items():
        if not baselines.get(url) or snapshot['observed_at']>=baselines[url]['observed_at']: baselines[url]=snapshot
    state={'records':records,'baselines':baselines,'report':result['report'],
           'search_cursor':result.get('search_cursor',previous.get('search_cursor'))}
    candidates=merge_candidates(previous.get('source_candidates',[]), result.get('source_candidates',[]))
    decisions=json.loads((Path(__file__).resolve().parents[1]/'collect/source-reviews.json').read_bytes())
    candidates=apply_reviews(candidates,decisions)
    candidates,deferred=moderation_queue(previous.get('source_candidates',[]),candidates)
    state['report']={**result['report'],'deferred_new_source_reviews':deferred}
    state.update(source_candidates=candidates,site_cursor=result.get('site_cursor',previous.get('site_cursor')),
                 discovery_cooldowns=result.get('discovery_cooldowns',previous.get('discovery_cooldowns',{})),
                 validation_cursors=result.get('validation_cursors',previous.get('validation_cursors',{})))
    raw=encoded(state)
    if len(raw)>MAX_JSON: raise ValueError('discovery_state_needs_sharding')
    digest=hashlib.sha256(raw).hexdigest(); name='discovery-state-'+digest+'.json'
    store.put(name,raw)
    store.json('discovered-links.json',{'records':records},mutable=True)
    store.put('discovered-links.txt',render(records),mutable=True)
    store.json('discovery-report.json',state['report'],mutable=True)
    store.json('discovery-catalog.json',{'format':'argus-discovery-catalog-v1','snapshot':name,'sha256':digest},mutable=True)
    if load_state(store)!=state: raise ValueError('discovery_readback_mismatch')
    if candidates:
        shipped=json.loads((Path(__file__).resolve().parents[1]/'collect/sources.json').read_bytes())
        catalog=store.read_json('sources.json') or shipped
        # A shipped source absent from a lagging Drive catalog already has a stable ID.
        from source_origin import catalog_url
        known={catalog_url(s['url']) for s in shipped['sources']}
        updated=enroll(catalog,[r for r in candidates if r['platform'] not in disabled and catalog_url(r['url']) not in known])
        if updated!=catalog:
            store.json('sources.json',updated,mutable=True)
            if store.read_json('sources.json')!=updated:raise ValueError('source_registry_readback_mismatch')
    store.json('source-review.json',{'counts':source_counts(candidates),
               'candidates':[r for r in candidates if r['status']=='review']},mutable=True)
    return state


def prepare(store,root):
    store.assert_private(); state=load_state(store); snapshot=load_snapshot(store)
    fields=('id','platform','url','text','observed_at','sample','outbound_social_links','outbound_profile_links')
    posts=[{k:p[k] for k in fields if k in p} for p in snapshot['items'] if not p.get('sample') and p['platform'] in ('telegram','rss','mailru')]
    data={'posts':posts,'baselines':state['baselines'],'search_cursor':state.get('search_cursor'),
          'site_cursor':state.get('site_cursor'),'discovery_cooldowns':state.get('discovery_cooldowns',{}),
          'validation_cursors':state.get('validation_cursors',{}),
          'registered_discoveries':registered_discoveries(store.read_json('sources.json')),
          'directory_sites':[r['url'] for r in state.get('source_candidates',[]) if r['method'] in
                             ('government_directory','media_directory') and r['status']!='rejected']}
    (root/'input.json').write_bytes(encoded(data))
    print(json.dumps({'archived_posts_for_discovery':len(posts),'previous_baselines':len(state['baselines'])}))


def main():
    require_cloud(); parser=argparse.ArgumentParser(); parser.add_argument('action',choices=['prepare','publish']); args=parser.parse_args()
    root=Path('collection/discovery'); root.mkdir(parents=True,exist_ok=True)
    store=from_environment()
    if args.action=='prepare': return prepare(store,root)
    result=json.loads((root/'result.json').read_text(encoding='utf-8'))
    imported_run=os.environ.get('ARGUS_IMPORT_RUN','')
    pins=json.loads(Path('ops/collect/discovery-upstreams.json').read_text())
    if result['report'].get('upstreams')!=pins: raise ValueError('discovery_parser_revision_mismatch')
    if imported_run and (not re.fullmatch(r'\d+',imported_run) or result['report']['run_id']!=imported_run):
        raise ValueError('discovery_run_mismatch')
    from registry import validate
    registry=validate(json.loads(Path('registry/collectors.json').read_text()))
    disabled={r['platform'] for r in registry['collectors'] if not r['enabled']}
    result['records']=[r for r in result['records'] if r['platform'] not in disabled]
    # Imported post-link artifacts do not authorize changes to the source registry.
    if imported_run:
        result.pop('source_candidates',None)
        result.pop('site_cursor',None)
        result.pop('discovery_cooldowns',None)
        result.pop('validation_cursors',None)
    else:
        result['source_candidates']=[r for r in result.get('source_candidates',[]) if r['platform'] not in disabled]
    archive_before=store.read_json('catalog.json')
    state=publish_discovery(store,result,disabled); pointer=store.read_json('discovery-catalog.json')
    replay=publish_discovery(store,result,disabled)
    if replay!=state or store.read_json('discovery-catalog.json')!=pointer: raise ValueError('discovery_replay_changed')
    if store.read_json('catalog.json')!=archive_before: raise ValueError('post_archive_changed_by_discovery')
    mapping=json.loads(private_asset_path('platform-folders.json').read_text(encoding='utf-8'))
    if mapping['root']!=store.root: raise ValueError('root_mismatch')
    for platform in ('facebook','instagram'):
        folder=mapping['platforms'][platform]
        info=store.service.files().get(fileId=folder['folder_id'],fields='parents,name').execute(num_retries=3)
        if store.root not in info.get('parents',[]) or info.get('name')!=folder['name']: raise ValueError('unexpected_platform_folder')
        view=DriveStore(store.service,folder['folder_id']); view.assert_private()
        rows=[r for r in state['records'] if r['platform']==platform]
        view.json('discovered-links.json',{'records':rows},mutable=True)
        view.put('discovered-links.txt',render(rows),mutable=True)
    verification={'links':len(state['records']),'baselines':len(state['baselines']),'replay':'unchanged','post_archive':'unchanged'}
    (root/'verification.json').write_text(json.dumps(verification),encoding='utf-8'); print(json.dumps(verification))


if __name__=='__main__': main()
