"""Validate every shard and reconcile every enabled source before publication."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'local-storage'))
from storage import validate_package,pack
from run import PLATFORMS,now
from record_quality import merge_observations
from source_health import SKIPPED, platform_status

def merge(directory,catalog_path,output,shards,registry_path=None):
    root=Path(directory);catalog=json.loads(Path(catalog_path).read_text(encoding='utf-8'))
    if len({s['id'] for s in catalog['sources']})!=len(catalog['sources']):
        raise ValueError('Repeated source ID in catalog')
    expected={s['id'] for s in catalog['sources'] if s.get('enabled',s.get('status','active')=='active')}
    digest=hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest()
    seen=set();posts={};results=[];reports=[];state_updates={}
    mismatched_shards=[]
    registry_digest=hashlib.sha256(Path(registry_path).read_bytes()).hexdigest() if registry_path else None
    for index in range(shards):
        directory=root/f'argus-platform-part-{index}'
        report=json.loads((directory/'report.json').read_text(encoding='utf-8'))
        if report['shard_index']!=index or report['shard_count']!=shards or report['catalog_sha256']!=digest:
            raise ValueError('Mismatched shard or catalog')
        if report.get('registry_sha256')!=registry_digest:raise ValueError('Mismatched collector registry')
        for result in report['sources']:
            identity=result['source_id']
            if identity not in expected or identity in seen:raise ValueError('Unexpected or repeated source result')
            seen.add(identity);results.append(result)
        with tempfile.TemporaryDirectory() as temp:
            with validate_package(directory/'package.zip',temp) as (manifest,items,_):
                if manifest['sample']:raise ValueError('Sample shard rejected')
                if all('canonical_keys' in r for r in report['sources']):
                    measured={k for r in report['sources'] for k in r['canonical_keys']}
                    if measured!={item['canonical_key'] for item in items}:
                        mismatched_shards.append(index)
                for item in items:
                    if item['media']:raise ValueError('Binary media are not part of this collection mode')
                    old=posts.get(item['canonical_key'])
                    posts[item['canonical_key']]=merge_observations(old,item)
        reports.append(report)
        for key,value in report.get('state_updates',{}).items():
            if key in state_updates or key not in {s['source_id'] for s in report['sources']}:raise ValueError('Invalid cursor shard')
            state_updates[key]=value
    if seen!=expected:raise ValueError('Missing enabled sources')
    if mismatched_shards:raise ValueError(f'Shard package differs from source records: {mismatched_shards}')
    # The same Facebook story may be returned with a numeric ID and an opaque
    # permalink. Join only when a reader supplied the matching native story ID.
    from url_identity import identity_keys
    aliases={}; grouped={}
    for key,item in sorted(posts.items()):
        keys=identity_keys(item)
        canonical=next((aliases[k] for k in keys if k in aliases),key)
        for k in keys:aliases[k]=canonical
        row=merge_observations(grouped.get(canonical),{**item,'canonical_key':canonical})
        linked=set(row.get('identity_aliases',[])).union(keys)-{canonical}
        if linked:row['identity_aliases']=sorted(linked)
        grouped[canonical]=row
    posts=grouped
    for result in results:
        if 'canonical_keys' in result:
            result['canonical_keys']=sorted({aliases.get(k,k) for k in result['canonical_keys']})
        for attempt in result.get('attempts',[]):
            if 'keys' in attempt:
                attempt['keys']=sorted({aliases.get(k,k) for k in attempt['keys']})
    summaries=[]
    for platform in PLATFORMS:
        if catalog.get('platform_scope') and platform not in catalog['platform_scope']:continue
        configured=[s for s in catalog['sources'] if s['platform']==platform]
        entries=[r for r in results if r['platform']==platform]
        attempted=[r for r in entries if r['status'] not in SKIPPED]
        count=sum(p['platform']==platform for p in posts.values())
        summaries.append({'platform':platform,'configured':len(configured),'enabled':len(entries),'selected':len(attempted),
            'disabled':len(configured)-len(entries),'deferred':sum(r['status'] in SKIPPED-{'disabled_by_registry'} for r in entries),'posts':count,
            'results':dict(collections.Counter(r['status'] for r in entries)),
            'status':platform_status(configured,(r['status'] for r in entries),count)})
    report={'format':'argus-collection-report-v1','run_id':reports[0]['run_id'],'updated_at':now(),
            'wall_seconds':sum(r['wall_seconds'] for r in reports),'media_mode':'links_only',
            'catalog_provenance':catalog['provenance'],'catalog_sha256':digest,'platforms':summaries,'sources':results,
            'unique_posts':len(posts),'attempted_sources':sum(r['status'] not in SKIPPED for r in results),'shards':shards,'registry_sha256':registry_digest,
            'confirmed_dates':sum(bool(p.get('published_at')) for p in posts.values()),
            'full_texts':sum(bool(p['text']) and p.get('text_may_be_truncated') is False for p in posts.values()),
            'state_updates':state_updates,'full_history_verified':False}
    if catalog.get('platform_scope'): report['platform_scope']=catalog['platform_scope']
    from dsns_coverage import coverage
    report['dsns_coverage']=coverage(results,list(posts.values()),report['updated_at'])
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    (out/'sources.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    (out/'posts.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in posts.values()),encoding='utf-8')
    (out/'media').mkdir(exist_ok=True)
    # Replaying immutable shards must produce the same package identity, even
    # if the first writer stopped after publishing but before saving cursors.
    collected_at=max((r.get('updated_at','') for r in reports),default='')
    pack(out/'posts.jsonl',out/'media',out/'package.zip',created_at=collected_at)
    print(json.dumps({'sources_reconciled':len(seen),'unique_posts':len(posts),'platforms':summaries}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--catalog',required=True);p.add_argument('--output',required=True);p.add_argument('--shards',type=int,required=True);p.add_argument('--registry');a=p.parse_args()
    merge(a.input,a.catalog,a.output,a.shards,a.registry)
