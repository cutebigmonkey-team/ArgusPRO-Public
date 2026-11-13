"""Verify a discovery sample through the registry, planner, collector and writer in isolated storage."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.append(str(Path(__file__).resolve().parents[1]/'collect'))
from catalog_scope import due_batch
from feed_history import collect
from publish import load_snapshot
from publish_incremental import drain
from record_quality import merge_observations
from run import now
from social_discovery import publish_discovery
from source_discovery import registered_discoveries
from storage import pack
from test_publish import MemoryStore


def audit(sample):
    store=MemoryStore()
    result={'records':[],'report':{},'baselines':{},'source_candidates':sample['candidates']}
    publish_discovery(store,result)
    catalog=store.read_json('sources.json')
    sources=registered_discoveries(catalog)
    selection=due_batch({'sources':sources},{},{'rss','mailru'},datetime.now(timezone.utc))
    posts={}
    rows=[]
    updates={}
    for source in selection['sources']:
        start=time.monotonic()
        items,status,attempts,cursor=collect(source,{},max_pages=2,seconds=30)
        if cursor:
            updates[source['id']]=cursor
        rows.append({'source_id':source['id'],'url':source['url'],'platform':source['platform'],
                     'checked_at':now(),'seconds':round(time.monotonic()-start,2),'status':status,
                     'posts':len(items),'attempts':attempts,'canonical_keys':[p['canonical_key'] for p in items]})
        for item in items:
            key=item['canonical_key']
            posts[key]=merge_observations(posts.get(key),item)
        if status in ('http_429','http_503'):
            break
        time.sleep(1)
    report={'run_id':'local-audit','incremental':True,'sources':rows,'updated_at':now(),'state_updates':updates}
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        (root/'media').mkdir()
        body=root/'posts.jsonl'
        body.write_text(''.join(json.dumps(p)+'\n' for p in posts.values()),encoding='utf-8')
        path=root/'sample.zip'
        pack(body,root/'media',path)
        drain(store,path,report)
        first=load_snapshot(store)
        drain(store,path,report)
        replay=load_snapshot(store)
    keys={p['id'] for p in replay['items']}
    if not set(posts)<=keys or replay!=first:
        raise ValueError('sample_readback_or_replay_mismatch')
    return {'verified_at':now(),'storage':'isolated_memory_store','catalog_total':len(catalog['sources']),
            'scheduled_new_feeds':len(selection['sources']),'sources':[{'url':r['url'],'status':r['status'],
            'read':r['posts'],'keys_in_archive':len(set(r['canonical_keys']) & keys)} for r in rows],
            'unique_archived':len(keys),'origins':sorted({p['origin'] for p in replay['items']}),
            'saved_cursors':len(store.read_json('collection-cursors.json').get('incremental_sources',{})),
            'replay_unchanged':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('sample',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    receipt=audit(json.loads(args.sample.read_bytes()))
    args.output.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps(receipt))
