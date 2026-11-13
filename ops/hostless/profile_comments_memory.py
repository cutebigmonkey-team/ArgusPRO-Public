"""Offline archive-sized comment restart workload; no production credentials.

Run in separate processes for each implementation. SQLite exercises the real
sync code but does not model libpq, Chromium, or production archive text sizes.
--implementation can select an extracted baseline comments_store.py.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import platform
import resource
import sqlite3
import sys
import tempfile

OPS=Path(__file__).resolve().parents[1]
for folder in ('hostless','drive','collect','local-storage'):
    sys.path.insert(0,str(OPS/folder))
from memory import resident_bytes,release
from analytics_snapshot import AnalyticsIndex


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation',type=Path,default=Path(__file__).with_name('comments_store.py'))
    parser.add_argument('--posts',type=int,default=50924)
    parser.add_argument('--text-repeats',type=int,default=100)
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('profiled_comments',args.implementation)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    samples=[]
    def sample(phase):
        samples.append(dict(phase=phase,rss_bytes=resident_bytes(),
            peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024))
    posts=[dict(id=str(i),platform='telegram',source_id=str(i%1317),
        url='https://t.me/source/'+str(i),text=(str(i)+' Публікація 🔥 ')*args.text_repeats,
        published_at='2026-11-08T13:00:00Z',origin='general') for i in range(args.posts)]
    sample('archive_objects')
    analytics=AnalyticsIndex(posts);sample('analytics');release();sample('analytics_trimmed')
    with tempfile.TemporaryDirectory(prefix='argus-profile-') as directory:
        db=sqlite3.connect(str(Path(directory)/'state.db'));db.row_factory=sqlite3.Row
        try:
            comments=module.Comments(db,sqlite=True);sample('comments_init')
            comments.sync(posts);sample('first_sync');release();sample('first_sync_trimmed')
            db.execute("UPDATE comment_jobs SET cursor='retained' WHERE id='0'");db.commit()
            del comments
            comments=module.Comments(db,sqlite=True);comments.sync(iter(posts));sample('restart_sync')
            release();sample('restart_trimmed')
            assert db.execute('SELECT COUNT(*) FROM comment_jobs').fetchone()[0]==args.posts
            assert db.execute("SELECT cursor FROM comment_jobs WHERE id='0'").fetchone()[0]=='retained'
            assert len(analytics.rows)==args.posts
        finally:db.close()
    print(json.dumps(dict(python=platform.python_version(),posts=args.posts,
        sources=1317,text_repeats=args.text_repeats,synthetic=True,samples=samples),indent=2))


if __name__=='__main__':main()
