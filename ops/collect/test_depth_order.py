import unittest
from datetime import datetime,timezone
from catalog_scope import due_batch, history_batch
from news_order import pending_news


class NewsOrder(unittest.TestCase):
    def test_clustered_ids_cannot_crowd_due_platforms_out_of_live(self):
        now=datetime(2026,11,3,10,1,tzinfo=timezone.utc)
        platforms=['rss']*40+['facebook','instagram','x','threads','youtube','tiktok']
        sources=[dict(id=f'{i:03}',url=f'https://example.org/{i}',platform=p) for i,p in enumerate(platforms)]
        batch=due_batch({'sources':sources},{},set(platforms),now,12)['sources']
        self.assertEqual(len({s['id'] for s in batch}),12)
        self.assertEqual({s['platform'] for s in batch},set(platforms))
        self.assertEqual(batch[0],sources[0])

    def test_second_slot_for_a_productive_source_keeps_oldest_slot(self):
        now=datetime(2026,11,3,10,1,tzinfo=timezone.utc)
        sources=[dict(id=str(i),url=f'https://example.org/{i}',platform='facebook') for i in range(6)]
        prior={'polling':{'5':dict(url=sources[5]['url'],discovered_posts=3)}}
        plan=due_batch({'sources':sources},prior,{'facebook'},now,3)
        self.assertEqual(plan['live_order'],['0','5','1'])

    def test_due_general_facebook_profiles_get_a_turn_alongside_official_pages(self):
        now=datetime(2026,11,3,10,1,tzinfo=timezone.utc)
        sources=[dict(id=str(i),url=f'https://facebook.com/{i}',platform='facebook',
                      origin='dsns' if i<5 else 'general') for i in range(6)]
        plan=due_batch({'sources':sources},{},{'facebook'},now,2)
        self.assertEqual(plan['live_order'],['0','5'])

    def test_live_slots_balance_both_feeds_throughout_the_batch(self):
        now = datetime(2026, 11, 3, 10, 1, tzinfo=timezone.utc)
        platforms = ('telegram', 'facebook', 'instagram', 'youtube')
        sources = [dict(id=f'{p}-{i:02}', platform=p, url=f'https://example.org/{p}/{i}',
                        origin='dsns' if i < 10 else 'general')
                   for p in platforms for i in range(20)]
        batch = due_batch({'sources': sources}, {}, set(platforms), now, 24)['sources']
        for platform in platforms:
            chosen = [s for s in batch if s['platform'] == platform]
            self.assertEqual(len(chosen), 6)
            self.assertEqual(sum(s['origin'] == 'general' for s in chosen), 3)

    def test_live_lanes_cover_each_platform_once_and_acknowledge_separately(self):
        from catalog_scope import live_platforms, recent_live_plan
        now = datetime(2026, 11, 3, 10, 5, tzinfo=timezone.utc)
        enabled = {'telegram', 'rss', 'youtube', 'mailru', 'x'}
        feeds = live_platforms(enabled, 'feeds')
        platforms = live_platforms(enabled, 'platforms')
        self.assertEqual(feeds, {'telegram', 'rss', 'mailru'})
        self.assertFalse(feeds & platforms)
        self.assertEqual(feeds | platforms, enabled)
        state = {'live_plans': {'feeds': now.isoformat()}, 'last_live_planned_at': now.isoformat()}
        self.assertTrue(recent_live_plan(state, now, 'feeds'))
        self.assertFalse(recent_live_plan(state, now, 'platforms'))

    def test_access_wall_prefers_configured_alternate_without_changing_its_origin(self):
        now=datetime(2026,11,3,10,5,tzinfo=timezone.utc)
        site=dict(id='web',name='Publisher',url='https://example.org/',platform='rss',origin='dsns')
        channel=dict(id='z',name='Publisher',url='https://t.me/publisher',platform='telegram',origin='dsns')
        sources=[site,channel]+[dict(channel,id=str(i),name='Different',url='https://t.me/foo'+str(i)) for i in range(10)]
        state={'incremental_polling':{'web':dict(url=site['url'],collector_status='robots_challenge_required',
                last_checked_at=now.isoformat(),next_check_at='2026-11-04T10:05:00Z')}}
        plan=due_batch({'sources':sources},state,{'telegram','rss'},now,6)
        self.assertEqual(plan['publisher_alternatives'],{'z':['web']})
        self.assertIn(channel,plan['sources'])
        self.assertNotIn(site,plan['sources'])

    def test_long_collection_does_not_consume_the_next_timer_tick(self):
        from catalog_scope import recent_live_plan
        now=datetime(2026,11,3,10,5,tzinfo=timezone.utc)
        state={'last_live_commit_at':'2026-11-03T10:04:01Z'}
        self.assertTrue(recent_live_plan(state,now))
        state['last_live_planned_at']='2026-11-03T10:00:00Z'
        self.assertFalse(recent_live_plan(state,now))
        state['last_live_planned_at']='2026-11-03T10:03:00Z'
        self.assertTrue(recent_live_plan(state,now))

    def test_only_owner_scoped_server_continuations_are_reusable(self):
        from browser_public import continuation
        owner='https://m.vk.com/wall-42'
        self.assertEqual(continuation(owner,'?offset=100'),owner+'?offset=100')
        for target in ('https://other.example/wall-42?offset=100','/wall-99?offset=100','?id=99&offset=100','/login'):
            self.assertIsNone(continuation(owner,target))
    def test_original_publication_time_precedes_later_repost_and_backlog_gets_a_slot(self):
        now=datetime(2026,11,3,10,1,tzinfo=timezone.utc)
        sources=[dict(id=k,platform=p,url='https://example.org/'+k) for k,p in
                 [('unseen','rss'),('repost','telegram'),('original','rss')]]
        def record(source,hour):
            return dict(id='post-'+source['id'],source_id=source['id'],source_url=source['url'],
                        published_at=f'2026-11-03T{hour}:02:00+00:00')
        posts=[dict(record(sources[1],'09'),published_at='2026-11-03T09:44:00Z'),record(sources[2],'09'),
               record(sources[0],'08'),dict(record(sources[0],'09'),published_at_is_estimated=True)]
        news=pending_news(posts,{},now)
        plan=due_batch({'sources':sources},{'first_news':news},{'rss','telegram'},now,3)
        self.assertEqual(plan['live_order'][0],'original')
        self.assertEqual(set(plan['live_order']),{s['id'] for s in sources})
        self.assertEqual(plan['news_signals']['original']['post_id'],'post-original')
        old={'original':{'url':sources[2]['url'],'last_success_at':now.isoformat()}}
        self.assertNotIn('original',pending_news(posts,old,now))

    def test_archive_lane_respects_limits_and_rotates_independently(self):
        now=datetime(2026,11,3,11,tzinfo=timezone.utc)
        sources=[dict(id=k,platform='telegram',url='https://t.me/'+k) for k in ('aaa','bbb')]
        state={'history_polling':{'aaa':dict(url=sources[0]['url'],last_checked_at=now.isoformat())}}
        self.assertEqual(history_batch({'sources':sources},state,{'telegram'},now,1)['sources'],[sources[1]])
        state['history_polling']['aaa'].update(collector_status='http_429',next_check_at='2026-11-04T11:00:00Z')
        self.assertEqual(history_batch({'sources':sources},state,{'telegram'},now,1)['sources'],[])
        state['history_polling']['aaa']['last_checked_at']='2026-11-03T08:00:00Z'
        self.assertEqual(history_batch({'sources':sources},state,{'telegram'},now,1)['sources'],[sources[1]])
        state['history_polling']['aaa']['host_cooldown_until']='2026-11-04T09:00:00Z'
        self.assertEqual(history_batch({'sources':sources},state,{'telegram'},now,1)['sources'],[])

    def test_cooling_platform_only_admits_an_independent_publisher_copy(self):
        now=datetime(2026,11,3,11,tzinfo=timezone.utc)
        sources=[dict(id=k,platform='tiktok',url='https://www.tiktok.com/@'+k) for k in ('owned','other')]
        sources[0]['references']=[{'kind':'publisher_embed','url':'https://example.org/story'}]
        state={'polling':{'other':{'url':sources[1]['url'],'collector_status':'http_429','next_check_at':'2026-11-04T11:00:00Z'}}}
        batch=history_batch({'sources':sources},state,{'tiktok'},now)
        self.assertEqual(batch['sources'],sources[:1])
        self.assertIn('tiktok',batch['cooling_platforms'])
        live=due_batch({'sources':sources},state,{'tiktok'},now)
        self.assertEqual(live['sources'],sources[:1])
        self.assertIn('tiktok',live['cooling_platforms'])
