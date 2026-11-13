"""Integration checks against an explicitly disposable database, never production."""
import os
import sys
import unittest
import uuid
from pathlib import Path
for folder in ('hostless','drive','collect','local-storage'):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/folder))
from comments_store import Comments,iso


@unittest.skipUnless(os.getenv('ARGUS_COMMENTS_TEST_DSN'),'disposable PostgreSQL test DSN not configured')
class PostgreSQLComments(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.db=psycopg.connect(os.environ['ARGUS_COMMENTS_TEST_DSN'],autocommit=True,row_factory=dict_row)
        self.addCleanup(self.db.close)
        if self.db.execute('SELECT current_database() AS name').fetchone()['name']!='argus_comments_test':
            self.fail('Integration checks require database argus_comments_test')
        self.schema='comments_test_'+uuid.uuid4().hex
        self.db.execute('CREATE SCHEMA '+self.schema)
        self.addCleanup(lambda:self.db.execute('DROP SCHEMA '+self.schema+' CASCADE'))
        self.db.execute('SET search_path TO '+self.schema)
        self.now=1793880000
        self.comments=Comments(self.db,clock=lambda:self.now)
        self.post=dict(id='p',platform='telegram',source_id='s',url='https://t.me/channel/1',published_at=iso(self.now))
        self.comments.sync([self.post])

    def test_restart_pagination_and_health(self):
        job=self.comments.due()[0]
        self.comments.acknowledge(job,dict(status='partial',rows=[dict(id='a',text='Root')],cursor={'before':'10'}))
        self.now+=31
        resumed=Comments(self.db,clock=lambda:self.now)
        self.assertIn('before',resumed.due()[0]['cursor'])
        resumed.acknowledge(resumed.due()[0],dict(status='success',rows=[dict(id='b',text='Reply',parent_id='a')]))
        self.assertEqual(resumed.metrics()['comments'],2)
        self.assertEqual(resumed.metrics()['platforms']['telegram']['statuses'],{'success':1})
        self.assertIsNone(self.db.execute('SELECT cursor FROM comment_jobs').fetchone()['cursor'])

    def test_database_failure_rolls_back_comments_and_checkpoint(self):
        # Simulate failure after writing comments but before acknowledging the job.
        self.db.execute("ALTER TABLE comment_jobs ADD CONSTRAINT reject_success CHECK(status <> 'success')")
        with self.assertRaises(Exception):
            self.comments.acknowledge(self.comments.due()[0],dict(status='success',rows=[dict(id='a',text='Must roll back')]))
        self.assertEqual(self.comments.metrics()['comments'],0)
        self.assertEqual(self.comments.due()[0]['status'],'pending')
        self.assertEqual(self.db.execute('SELECT COUNT(*) AS n FROM comment_versions').fetchone()['n'],0)

    def test_chunked_sync_preserves_checkpoint_on_restart(self):
        posts=[{**self.post,'id':str(i),'url':self.post['url']+'/'+str(i)} for i in range(300)]
        self.comments.sync(posts)
        self.db.execute("UPDATE comment_jobs SET cursor='saved',checked=123,status='partial' WHERE id='150'")
        resumed=Comments(self.db,clock=lambda:self.now)
        resumed.sync(iter(posts))
        row=self.db.execute("SELECT * FROM comment_jobs WHERE id='150'").fetchone()
        self.assertEqual((row['cursor'],row['checked'],row['status']),('saved',123,'partial'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) AS n FROM comment_jobs').fetchone()['n'],301)

    def test_later_chunk_failure_rolls_back_all_chunks_and_cache(self):
        self.db.execute("ALTER TABLE comment_jobs ADD CONSTRAINT reject_chunk CHECK(id <> '200')")
        posts=[{**self.post,'id':str(i)} for i in range(300)]
        before=dict(self.comments.synced)
        with self.assertRaises(Exception):self.comments.sync(posts)
        self.assertEqual(self.comments.synced,before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) AS n FROM comment_jobs').fetchone()['n'],1)
        self.db.execute('ALTER TABLE comment_jobs DROP CONSTRAINT reject_chunk')
        self.comments.sync(posts)
        self.assertEqual(self.db.execute('SELECT COUNT(*) AS n FROM comment_jobs').fetchone()['n'],301)


if __name__=='__main__':unittest.main()
