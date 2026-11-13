"""Cross-language fixture: SQL pending -> real journal -> actual frontend API code.

No platform/Drive network calls, production state, browser or synthetic uploads.
Actual C++/WASM rendering remains a separate required acceptance gate.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime,timezone
from types import SimpleNamespace
OPS=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(OPS/x) for x in ('hostless','drive','collect','local-storage')]
from test_continuous import Session
from test_publish import MemoryStore
from worker import Pending,assemble
from schedule_store import ScheduleStore,iso
from indexed_journal import IndexedFeedJournal
from feed_journal import read_page


def verify(ui_root):
    with tempfile.TemporaryDirectory(prefix='argus-api-fixture-') as temporary:
        root=Path(temporary);db=sqlite3.connect(root/'state.sqlite');db.row_factory=sqlite3.Row
        store=MemoryStore();queue=ScheduleStore(db,sqlite=True)
        sources=[{'id':name,'platform':'rss','url':'https://fixture.invalid/'+name,'origin':name}
                 for name in ('general','dsns','unknown')]
        catalog={'sources':sources};store.json('sources.json',catalog);queue.sync(catalog,{}, {'rss'})
        feed=IndexedFeedJournal(store)
        try:
            now=datetime.now(timezone.utc);stamp=now.isoformat()
            for source in queue.claim(now.timestamp(),limit=3):
                key='rss:fixture-'+source['id']
                post={'canonical_key':key,'platform':'rss','post_url':source['url']+'/post','text':'Synthetic public-media fixture',
                    'published_at':None if source['id']=='unknown' else '2020-01-01T00:00:00Z',
                    'observed_at':stamp,'source_id':source['id'],'source_url':source['url'],'origin':source['origin'],'media':[]}
                row={'source_id':source['id'],'url':source['url'],'platform':'rss','status':'ok','seconds':.01,
                     'checked_at':stamp,'canonical_keys':[key],'new_keys':[],'attempts':[],'rejections':[]}
                queue.stage({'token':source['_lease_token'],'record':row,'posts':[post]},now.timestamp())
            session=Session(db);journal=SimpleNamespace(db=SimpleNamespace(connection=session),check_session=lambda:None)
            pending=Pending(journal,queue,feed);package,report=assemble(queue,root);pending.stage(package,report)
            # Durable pending exists before any archive write.
            if pending.prepare() is None or len(queue.pending())!=3:raise AssertionError('fixture_pending_missing')
            pending.recover(store)
            if pending.prepare() is not None or queue.pending():raise AssertionError('fixture_ack_not_cleaned')
            page=read_page(store,feed.head)
            fixture=root/'frontend.json';fixture.write_text(json.dumps({'base':{'items':[],'packages':[]},'head':feed.head,'page':page}))
            script="""
import fs from 'node:fs';import assert from 'node:assert/strict';import {pathToFileURL} from 'node:url';
const [ui,fixture]=process.argv.slice(1);
const {createJournal}=await import(pathToFileURL(ui+'/frontend/cloud/journal.mjs'));
const {createWorkspaceData}=await import(pathToFileURL(ui+'/frontend/cloud/workspace-data.mjs'));
const data=JSON.parse(fs.readFileSync(fixture,'utf8'));
const snapshot=await createJournal()(data.base,data.head,async()=>data.page);
const api=createWorkspaceData(snapshot),get=query=>JSON.parse(api('/api/posts?'+query)).body.posts;
const all=get('origin=all&sort=discovered_at&time_basis=discovered_at');
assert.equal(all.length,3);assert.equal(get('origin=dsns').length,1);assert.equal(get('origin=general').length,1);
assert.equal(all.find(p=>p.origin==='unknown').publishedAt,'');
console.log(JSON.stringify({api_visible_ids:all.map(p=>p.id),unknown_publication_preserved:true,
  dsns_partition_correct:true,rendered_ids:null,rendering_status:'NOT_VERIFIED'}));
"""
            result=subprocess.run(['node','--input-type=module','-e',script,str(ui_root),str(fixture)],capture_output=True,text=True,timeout=20,check=True)
            return {'scope':'SYNTHETIC SQL/Drive fixture; actual frontend journal/API modules, no browser',
                    'archive_ids':[p['id'] for p in feed.index.rows()],
                    'pending_after_ack':len(queue.pending()),'drive_head_verified':True,
                    'client':json.loads(result.stdout),'production_verified':False}
        finally:feed.close();db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--ui-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.write_text(json.dumps(verify(a.ui_root.resolve()),indent=2))
