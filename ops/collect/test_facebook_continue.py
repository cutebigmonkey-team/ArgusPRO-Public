import unittest
from unittest.mock import AsyncMock, patch
from facebook_continue import legacy_tail, resume


class TailTests(unittest.TestCase):
    def test_interleaved_discovery_and_frozen_seeds(self):
        urls=['https://www.facebook.com/example/posts/'+str(i) for i in range(1,25)]
        reads=[{'candidate':'facebook:'+str(i),'via':'posts_tab' if i==1 else 'archive_permalink',
                'status':'login_required'} for i in range(1,21)]
        record={'id':'s','stages':{'detail':'candidate_budget'},'anchor':None,'reads':reads,'candidates':24}
        plan={'seeds':{'s':[urls[0]]+urls}}
        self.assertEqual([r['key'] for r in legacy_tail(record,plan)],['facebook:'+str(i) for i in range(21,25)])
        reads[1],reads[2]=reads[2],reads[1]
        with self.assertRaisesRegex(ValueError,'original_queue'):
            legacy_tail(record,plan)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_live_fetch_two_separate_owner_checks(self):
        sources=[{'id':s,'name':s,'url':'https://www.facebook.com/'+s,'platform':'facebook'} for s in ('a','b')]
        key='facebook:123'; url=sources[0]['url']+'/posts/123'
        queue=[{'key':key,'url':url,'via':'archive_permalink'}]
        head={'key':'facebook:456','date':'2026-11-01T00:00:00+00:00','story_id':'456'}
        plan={'sources':sources,'anchors':{s['id']:head for s in sources},
              'remaining':{s['id']:queue for s in sources},'known':[key],'known_native':[]}
        raw={'post_id':'123','creation_time':1793318400,'comet_sections':{'content':{'story':{
            'post_id':'123','wwwURL':url,'message':{'text':'Допис'}}}}}
        ctx=AsyncMock();page=ctx.new_page.return_value;page.evaluate.return_value=[]
        with patch('facebook_continue.context_for',new=AsyncMock(return_value=ctx)),patch('facebook_continue.page_data',new=AsyncMock(return_value=([raw],[],[]))) as read:
            records,posts,total=await resume(object(),plan)
        self.assertEqual((total,read.await_count),(1,1))
        self.assertEqual([r['accepted'] for r in records],[[key],[]])
        self.assertTrue(posts[0]['facebook_known_before'])
        self.assertTrue(all(r['remaining_queue']==[] for r in records))
