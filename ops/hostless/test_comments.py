import json
import sqlite3
import sys
import unittest
import types
import urllib.error
from pathlib import Path
from unittest.mock import patch,Mock
for p in ('hostless','drive','collect','local-storage'):sys.path.insert(0,str(Path(__file__).resolve().parents[1]/p))
from comments_store import Comments,key
from comment_adapters import collect,youtube,wordpress,youtube_public
from test_publish import MemoryStore


class CommentTests(unittest.TestCase):
    def test_bounded_sync_restart_preserves_cursor_and_url_change_resets_only_changed_job(self):
        posts=[{**self.post,'id':str(i),'url':self.post['url']+'/'+str(i)} for i in range(300)]
        self.c.sync(posts)
        self.db.execute("UPDATE comment_jobs SET cursor='saved',checked=123,status='partial' WHERE id='150'")
        self.db.commit()
        queries=[];self.db.set_trace_callback(queries.append)
        resumed=Comments(self.db,sqlite=True,clock=lambda:self.now)
        resumed.sync(iter(posts))
        row=self.db.execute("SELECT * FROM comment_jobs WHERE id='150'").fetchone()
        self.assertEqual((row['cursor'],row['checked'],row['status']),('saved',123,'partial'))
        reads=[q for q in queries if q.startswith('SELECT id,post FROM comment_jobs')]
        self.assertEqual(len(reads),3)
        self.assertTrue(all('WHERE id IN (' in q for q in reads))
        resumed.sync([{**posts[150],'url':'https://example.com/changed'}])
        row=self.db.execute("SELECT * FROM comment_jobs WHERE id='150'").fetchone()
        self.assertIsNone(row['cursor'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_jobs').fetchone()[0],301)

    def test_later_chunk_failure_rolls_back_all_jobs_and_cache(self):
        posts=[{**self.post,'id':str(i)} for i in range(300)]
        self.db.execute("CREATE TRIGGER fail_chunk BEFORE INSERT ON comment_jobs WHEN NEW.id='200' BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.db.commit();before=dict(self.c.synced)
        with self.assertRaises(sqlite3.IntegrityError):self.c.sync(posts)
        self.assertEqual(self.c.synced,before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_jobs').fetchone()[0],1)
        self.db.execute('DROP TRIGGER fail_chunk');self.db.commit()
        self.c.sync(posts)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_jobs').fetchone()[0],301)

    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.now=1793880000;self.c=Comments(self.db,sqlite=True,clock=lambda:self.now)
        self.post=dict(id='p1',platform='facebook',url='https://www.facebook.com/test/posts/1',source_id='s',text='Post',published_at='2026-10-01T00:00:00Z')
        self.c.sync([self.post]);self.job=self.c.due()[0]
    def answer(self,rows,**kw):return dict(rows=rows,status='success',**kw)
    def test_edit_dedup_history_and_parent(self):
        rows=[dict(id='a',text='First'),dict(id='b',text='Reply',parent_id='a')]
        self.c.acknowledge(self.job,self.answer(rows));self.c.acknowledge(self.job,self.answer(rows))
        self.assertEqual(self.c.metrics()['comments'],2)
        row=json.loads(self.db.execute('SELECT payload FROM collected_comments WHERE id=?',(key('facebook','p1','b'),)).fetchone()[0])
        self.assertEqual(row['parent_id'],key('facebook','p1','a'))
        self.c.acknowledge(self.db.execute('SELECT * FROM comment_jobs').fetchone(),self.answer([dict(id='a',text='Edited')]))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_versions').fetchone()[0],3)
    def test_old_post_new_reply_restart_and_cursor(self):
        self.c.acknowledge(self.job,dict(rows=[dict(id='a',text='One')],status='partial',cursor={'page':'next'}))
        self.now+=31
        resumed=Comments(self.db,sqlite=True,clock=lambda:self.now);job=resumed.due()[0]
        self.assertEqual(json.loads(job['cursor']),{'page':'next'})
        resumed.acknowledge(job,self.answer([dict(id='b',text='New today')]))
        self.assertEqual(resumed.metrics()['comments'],2)
        self.assertIsNone(self.db.execute('SELECT cursor FROM comment_jobs').fetchone()[0])
    def test_failure_retains_cursor_and_retry_after(self):
        self.db.execute('UPDATE comment_jobs SET cursor=?',(json.dumps({'page':'next'}),));self.db.commit()
        job=self.c.due()[0]
        self.c.acknowledge(job,dict(status='rate_limited',retry_after=9000))
        row=self.db.execute('SELECT * FROM comment_jobs').fetchone()
        self.assertEqual(row['cursor'],job['cursor']);self.assertGreaterEqual(row['due'],self.now+9000)
    def test_failed_publish_retains_pointer_then_recovers(self):
        store=MemoryStore();self.c.acknowledge(self.job,self.answer([dict(id='a',text='One')]))
        self.c.publish(store);old=store.read_json('comments-catalog.json')
        self.now+=61;self.c.acknowledge(self.db.execute('SELECT * FROM comment_jobs').fetchone(),self.answer([dict(id='b',text='Two')]))
        store.fail='comments-'
        with self.assertRaises(RuntimeError):self.c.publish(store)
        self.assertEqual(store.read_json('comments-catalog.json'),old)
        store.fail=None;self.c.publish(store)
        self.assertEqual(store.read_json('comments-catalog.json')['metrics']['comments'],2)
    def test_samples_disabled_and_missing_access(self):
        self.c.sync([{**self.post,'id':'sample','sample':True}],[])
        self.c.sync([self.post],[])
        self.assertEqual(self.c.due(),[])
        with patch.dict('os.environ',{},clear=True):
            with patch('comment_public.telegram_public',return_value={'status':'partial'}) as public:
                self.assertEqual(collect({**self.post,'platform':'telegram'})['status'],'partial');public.assert_called_once()
            with patch('comment_adapters.youtube_public',return_value={'status':'partial'}) as public:
                self.assertEqual(collect({**self.post,'platform':'youtube'})['status'],'partial');public.assert_called_once()
        self.assertEqual(collect({**self.post,'platform':'max'})['status'],'unsupported')
    def test_empty_verified_page_success(self):
        self.c.acknowledge(self.job,self.answer([]))
        self.assertEqual(self.c.metrics()['statuses'],{'success':1})
    def test_platform_turn_uses_index_and_preserves_due_order(self):
        posts=[{**self.post,'id':'fb-new','published_at':'2026-11-05T10:00:00Z'},
               {**self.post,'id':'tg-new','platform':'telegram','published_at':'2026-11-05T11:00:00Z'},
               {**self.post,'id':'fb-held','published_at':'2026-11-05T12:00:00Z'}]
        self.c.sync(posts)
        self.db.execute("UPDATE comment_jobs SET due=? WHERE id='fb-held'",(self.now+900,));self.db.commit()
        jobs=self.c.due(4)
        self.assertEqual([row['id'] for row in jobs],['tg-new','fb-new','p1'])
        self.assertNotIn('fb-held',{row['id'] for row in jobs})
        plans=[]
        original=self.c.sql
        def explain(query,args=()):
            if query.startswith('SELECT j.* FROM comment_jobs j'):
                plans.extend(row['detail'] for row in self.db.execute('EXPLAIN QUERY PLAN '+query,args))
            return original(query,args)
        with patch.object(self.c,'sql',side_effect=explain):self.c.due()
        self.assertTrue(any('comment_platform_turn' in plan for plan in plans),plans)
        self.assertFalse(any('SCAN comment_jobs' in plan or 'SCAN j' in plan for plan in plans),plans)
    def test_missing_public_excerpt_does_not_pause_other_posts(self):
        self.c.acknowledge(self.job,dict(status='auth_required',reason='public_comments_not_exposed'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_circuits').fetchone()[0],0)
        self.c.sync([{**self.post,'id':'p2'}])
        self.assertEqual(self.c.due()[0]['id'],'p2')
        # Previously created excerpt circuits are repaired, true 429s are retained.
        self.db.execute('INSERT INTO comment_circuits VALUES(?,?)',('facebook',self.now+86400));self.db.commit()
        Comments(self.db,sqlite=True,clock=lambda:self.now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_circuits').fetchone()[0],0)
        self.c.acknowledge(self.c.due()[0],dict(status='rate_limited',reason='http_429',retry_after=3600))
        Comments(self.db,sqlite=True,clock=lambda:self.now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_circuits').fetchone()[0],1)
    def test_youtube_nested_pagination(self):
        post={**self.post,'platform':'youtube','url':'https://www.youtube.com/watch?v=abcdefghijk'}
        first={'items':[{'snippet':{'topLevelComment':{'id':'a','snippet':{'textOriginal':'Top'}},'totalReplyCount':2}}],'nextPageToken':'page2'}
        with patch.dict('os.environ',{'ARGUS_YOUTUBE_API_KEY':'test'}),patch('comment_adapters.get',return_value=first):answer=youtube(post,None)
        self.assertEqual(answer['cursor'],{'page':'page2','replies':['a']})
        with patch.dict('os.environ',{'ARGUS_YOUTUBE_API_KEY':'test'}),patch('comment_adapters.get',return_value={'items':[{'id':'b','snippet':{'textOriginal':'Reply','parentId':'a'}}]}):second=youtube(post,answer['cursor'])
        self.assertEqual(second['rows'][0]['parent_id'],'a');self.assertEqual(second['cursor']['page'],'page2')
    def test_keyless_youtube_reader_bounds_results_and_retains_replies(self):
        session=Mock();session.request.return_value.status_code=200
        class Reader:
            def __init__(self):self.session=session
            def get_comments(self,video,**kwargs):
                self.session.request('GET','https://www.youtube.com/watch?v='+video)
                yield dict(cid='root.reply',reply=True,text='Response',votes='1.2K',time_parsed=1793880000)
                for n in range(250):yield dict(cid=str(n),text='Public',votes='12')
        module=types.SimpleNamespace(YoutubeCommentDownloader=Reader,SORT_BY_RECENT=1)
        original_request=session.request
        with patch.dict(sys.modules,{'youtube_comment_downloader':module}):
            answer=youtube_public(dict(url='https://youtu.be/abcdefghijk'),None)
        self.assertEqual(len(answer['rows']),200);self.assertEqual(answer['status'],'partial')
        self.assertEqual(answer['rows'][0]['parent_id'],'root');self.assertIsNone(answer['rows'][0]['likes'])
        self.assertEqual(answer['rows'][0]['published_at_precision'],'approximate')
        self.assertLessEqual(sum(original_request.call_args.kwargs['timeout']),8)
        session.close.assert_called_once()
    def test_keyless_youtube_keeps_partial_rows_and_retry_after(self):
        class Reader:
            def __init__(self):self.session=Mock()
            def get_comments(self,*args,**kwargs):
                yield dict(cid='one',text='Public')
                raise urllib.error.HTTPError('https://www.youtube.com',429,'limited',{'Retry-After':'120'},None)
        module=types.SimpleNamespace(YoutubeCommentDownloader=Reader,SORT_BY_RECENT=1)
        with patch.dict(sys.modules,{'youtube_comment_downloader':module}):
            answer=youtube_public(dict(url='https://www.youtube.com/watch?v=abcdefghijk'),None)
        self.assertEqual(answer['status'],'rate_limited');self.assertEqual(answer['retry_after'],120)
        self.assertEqual(len(answer['rows']),1)
    def test_wordpress_detects_endpoint_and_strips_html(self):
        post={**self.post,'platform':'rss','url':'https://example.org/news/'}
        html=b'<link rel="alternate" type="application/json" href="https://example.org/wp-json/wp/v2/posts/123">'
        data=json.dumps([dict(id=2,parent=1,content={'rendered':'<b>Hello</b>'},author_name='Reader',date_gmt='2026-11-05T10:00:00')]).encode()
        with patch('crawl_common.CrawlState.read',side_effect=[(html,post['url']),(data,'https://example.org/wp-json/wp/v2/comments')]):answer=wordpress(post,None)
        self.assertEqual(answer['rows'][0]['text'],'Hello');self.assertEqual(answer['rows'][0]['parent_id'],'1')
    def test_child_failure_does_not_drop_next_job(self):
        self.c.sync([{**self.post,'id':'p2','platform':'unsupported-platform'}])
        with patch('comments_store.subprocess.Popen',side_effect=OSError('test')):self.c.tick(lambda *a,**k:None)
        self.assertNotIn('pending',self.c.metrics()['statuses'])
    def test_upgrade_enables_public_platform_jobs_without_losing_limits(self):
        self.c.sync([{**self.post,'id':'tg','platform':'telegram'},{**self.post,'id':'th','platform':'threads'}])
        self.db.execute("UPDATE comment_jobs SET status='auth_required',reason='telegram_user_session_required',due=? WHERE id='tg'",(self.now+86400,))
        self.db.execute("UPDATE comment_jobs SET status='unsupported',reason='comment_adapter_not_available',due=? WHERE id='th'",(self.now+86400,))
        self.db.execute("INSERT INTO comment_circuits VALUES('telegram',?)",(self.now+86400,));self.db.commit()
        resumed=Comments(self.db,sqlite=True,clock=lambda:self.now)
        self.assertEqual({r['id'] for r in resumed.due(10)},{'p1','tg','th'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_circuits').fetchone()[0],0)
        self.c.acknowledge(next(r for r in resumed.due(10) if r['id']=='tg'),dict(status='rate_limited',reason='http_429',retry_after=3600))
        Comments(self.db,sqlite=True,clock=lambda:self.now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comment_circuits').fetchone()[0],1)
    def test_new_unchecked_posts_precede_older_backfill(self):
        self.now+=30
        self.c.sync([{**self.post,'id':'latest','published_at':'2026-11-05T12:00:00Z'}])
        self.assertEqual(self.c.due()[0]['id'],'latest')
    def test_publication_is_partitioned_and_unchanged_pages_reused(self):
        self.c.acknowledge(self.job,self.answer([dict(id=str(i),text='Comment '+str(i)) for i in range(230)]))
        store=MemoryStore();self.c.publish(store);catalog=store.read_json('comments-catalog.json')
        self.assertEqual(sum(p['count'] for p in catalog['pages']),230)
        self.assertTrue(all(p['count']<=100 for p in catalog['pages']))
        self.now+=61
        with patch.object(store,'put',wraps=store.put) as put:self.assertFalse(self.c.publish(store));put.assert_not_called()
        old={p['sha256'] for p in catalog['pages']}
        self.c.upsert(self.post,[dict(id='new',text='New comment')],self.now)
        self.now+=61
        self.c.publish(store)
        updated=store.read_json('comments-catalog.json')
        self.assertEqual(sum(p['count'] for p in updated['pages']),231)
        self.assertEqual(len({p['sha256'] for p in updated['pages']}-old),1)

if __name__=='__main__':unittest.main()
