"""Offline archive-scale measurement. No network, credentials or production SQL.

FileStore emulates Drive objects/checksums, NOT Google latency or availability.
An optional RLIMIT_AS is a process virtual-memory limit, not a cgroup/CPU gate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import sys
import tempfile
import time
from datetime import datetime,timezone

OPS=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(OPS/x) for x in ('hostless','collect','drive','local-storage')]
from archive_index import ArchiveIndex,Rows
from drive_store import encoded,MAX_JSON,read_parts,PARTS_FORMAT
from indexed_journal import IndexedFeedJournal
from publish import commit_snapshot
from storage import pack
from memory import measurement


class FileStore:
    def __init__(self,root):self.directory=Path(root);self.directory.mkdir();self.root='fixture_root_000000'
    def assert_private(self):pass
    def put(self,name,data,mutable=False):
        if not name or Path(name).name!=name:raise ValueError('invalid_fixture_name')
        path=self.directory/name
        if path.exists() and path.read_bytes()!=data and not mutable:raise ValueError('immutable_fixture_object')
        path.write_bytes(data)
        return 'file_'+hashlib.sha256(name.encode()).hexdigest()
    def json(self,name,value,mutable=False):return self.put(name,encoded(value),mutable)
    def read_bytes(self,name,limit):
        path=self.directory/name
        if not path.exists():return None
        if path.stat().st_size>limit:raise ValueError('fixture_object_too_large')
        return path.read_bytes()
    def read_json(self,name):
        raw=self.read_bytes(name,MAX_JSON)
        if raw is None:return None
        value=json.loads(raw)
        if isinstance(value,dict) and value.get('format')==PARTS_FORMAT:
            return read_parts(self,value['parts'],value['sha256'])
        return value


def benchmark(posts=50924,batches=20,per_batch=10):
    samples=[];latencies=[];start=time.monotonic()
    def sample(phase):samples.append({'phase':phase,**measurement()})
    def seed_digest(index):
        digest=hashlib.sha256();count=0
        for row in index.rows():
            if row['id'].startswith('rss:seed'):
                digest.update(encoded(row)+b'\n');count+=1
        return count,digest.hexdigest()
    with tempfile.TemporaryDirectory(prefix='argus-offline-benchmark-') as temporary:
        root=Path(temporary);store=FileStore(root/'drive');(root/'media').mkdir()
        catalog={'sources':[{'id':str(i),'platform':'rss','url':f'https://fixture.example/{i}',
                             'origin':'general','enabled':i<2} for i in range(1317)]}
        store.json('sources.json',catalog)
        seed=ArchiveIndex(str(root/'seed.sqlite'))
        sample('before_seed')
        text='Fixture publication. '+('abcdef0123456789 '*30)
        with seed.db:
            for i in range(posts):
                seed.put({'id':'rss:seed'+str(i),'platform':'rss','url':'https://fixture.example/post/'+str(i),
                    'text':text,'content':text,'source_id':str(i%1317),'origin':'general','files':[],
                    'published_at':None,'observed_at':'2026-11-01T10:00:00+00:00',
                    'discovered_at':'2026-11-01T10:00:00+00:00',
                    'public_comments':[{'id':'fixture-comment-'+str(i),'text':'Preserve archived metadata'}] if i%100==0 else []},initial=True)
        commit_snapshot(store,{'items':Rows(seed),'packages':[],'updated_at':'2026-11-01T10:00:00+00:00'})
        seed.close();(root/'seed.sqlite').unlink();sample('seed_streamed')
        feed=IndexedFeedJournal(store);sample('index_bootstrapped')
        original=feed.index.get('rss:seed0')
        original_digest=seed_digest(feed.index)
        try:
            for batch in range(batches):
                rows=[];records=[];now=datetime.now(timezone.utc).isoformat()
                source=catalog['sources'][batch%2]
                for i in range(per_batch):
                    key='rss:new'+str(batch*per_batch+i)
                    rows.append({'canonical_key':key,'platform':'rss','post_url':'https://fixture.example/new/'+key,
                        'text':'new fixture '+key,'published_at':None,'observed_at':now,
                        'source_id':source['id'],'source_url':source['url'],'media':[]})
                path=root/'new.jsonl';path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                package=root/'new.zip';pack(path,root/'media',package)
                report={'run_id':'fixture-'+str(batch),'updated_at':now,'incremental':True,'state_updates':{},
                    'sources':[{'source_id':source['id'],'platform':'rss','url':source['url'],'status':'ok',
                        'checked_at':now,'seconds':.1,'canonical_keys':[p['canonical_key'] for p in rows],
                        'new_keys':[],'attempts':[]}]}
                began=time.monotonic();feed.deliver(package,report);latencies.append(time.monotonic()-began)
                if report['new_to_archive']!=per_batch:raise AssertionError('fixture_insert_count')
                if batch in (0,batches-1):sample('delivery_'+str(batch))
            if feed.index.count()!=posts+batches*per_batch:raise AssertionError('fixture_archive_count')
            if feed.index.get('rss:seed0')!=original:raise AssertionError('fixture_old_record_changed')
            if seed_digest(feed.index)!=original_digest:raise AssertionError('fixture_seed_records_changed')
            feed.compact(force=True);sample('compacted')
            last=store.read_json('live-feed.json')
        finally:feed.close()
        recovered=IndexedFeedJournal(store)
        try:
            if recovered.index.count()!=posts+batches*per_batch or recovered.head!=last:raise AssertionError('fixture_restart')
            if seed_digest(recovered.index)!=original_digest:raise AssertionError('fixture_restart_seed_changed')
            sample('restarted')
        finally:recovered.close()
    total=sum(latencies)
    return {'scope':'OFFLINE synthetic archive with real normalization/journal/compaction; file-backed Drive fixture',
        'seed_posts':posts,'registered_fixture_sources':1317,'enabled_fixture_sources':2,
        'new_posts':batches*per_batch,'delivery_batches':batches,'duplicates':0,
        'old_record_preserved':True,'all_seed_records_preserved':True,'seed_records_sha256':original_digest[1],
        'restart_count_verified':True,'samples':samples,
        'file_fixture_posts_per_second':batches*per_batch/total,
        'file_fixture_delivery_seconds':{'min':min(latencies),'max':max(latencies),'mean':total/len(latencies)},
        'wall_seconds':time.monotonic()-start,'live_platform_throughput':None,'google_drive_latency':None,
        'actual_hostless_memory_limit':None,'container_gate':'NOT_VERIFIED','chromium_gate':'NOT_VERIFIED'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--posts',type=int,default=50924)
    parser.add_argument('--batches',type=int,default=20);parser.add_argument('--virtual-memory-mib',type=int,default=0)
    parser.add_argument('--browser',action='store_true')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.virtual_memory_mib:
        limit=args.virtual_memory_mib*1024**2;resource.setrlimit(resource.RLIMIT_AS,(limit,limit))
    if args.browser:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            try:
                context=browser.new_context();page=context.new_page()
                page.set_content('<html><body>'+('<article>Offline fixture card</article>'*1000)+'</body></html>')
                result=benchmark(args.posts,args.batches)
                context.close()
            finally:browser.close()
        result['chromium_gate']='TESTED_IN_FIXTURE';result['after_browser_cleanup']=measurement()
        if result['after_browser_cleanup']['child_processes']!=0:raise AssertionError('browser_children_remain')
    else:result=benchmark(args.posts,args.batches)
    result['process_virtual_memory_limit_mib']=args.virtual_memory_mib or None
    args.output.write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='samples'}))
