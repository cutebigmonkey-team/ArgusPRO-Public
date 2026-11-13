import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from catalog_scope import due_batch
from feed_history import collect


class IncrementalTests(unittest.TestCase):
    def test_channel_cache_is_scoped_expires_and_still_validates_atom(self):
        import youtube_history as history
        from datetime import timedelta
        channel='UCXoJ8kY9zpLBEz-8saaT3ew'
        source={'platform':'youtube','url':'https://www.youtube.com/@example'}
        previous={'url':source['url'],'channel':channel,'channel_resolved_at':datetime.now(timezone.utc).isoformat()}
        with patch.object(history,'resolve',return_value=channel) as resolver,patch.object(history,'fetch',return_value=(b'feed','url')),patch.object(history,'parse',return_value=[]) as parser:
            _,_,_,state=history.collect(source,previous,reuse_channel=True)
            resolver.assert_not_called();parser.assert_called_once_with(source,b'feed',channel,100)
            self.assertEqual(state['channel_resolved_at'],previous['channel_resolved_at'])
            for invalid in ({'url':'https://www.youtube.com/@another'},
                            {'channel_resolved_at':(datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()},
                            {'channel_resolved_at':'invalid'}):
                resolver.reset_mock();history.collect(source,{**previous,**invalid},reuse_channel=True)
                resolver.assert_called_once()

    def test_stale_instagram_cache_continues_to_public_profile(self):
        import asyncio
        from unittest.mock import AsyncMock
        from candidate_audit import publication_window
        from run import collect_existing
        source={'platform':'instagram','url':'https://www.instagram.com/example/'}
        old={'published_at':'2020-01-01T00:00:00Z'}
        recent={'published_at':datetime.now(timezone.utc).isoformat()}
        token=publication_window.set(datetime(2026,1,1,tzinfo=timezone.utc))
        try:
            with patch('rss_gateway.collect',return_value=[old]),patch('run.social',new=AsyncMock(return_value=[recent])):
                rows,status,attempts=asyncio.run(collect_existing(object(),source,100))
            self.assertEqual((rows,status),([recent],'ok'))
            self.assertEqual(attempts[0]['status'],'stale_feed_only')
        finally:publication_window.reset(token)

    def test_authorized_metadata_event_must_match_independently_verified_channel(self):
        import youtube_history as history
        channel='UCXoJ8kY9zpLBEz-8saaT3ew';video='QVI2ubEKAOA';url='https://www.youtube.com/watch?v='+video
        event={'format':'youtube-watch-microformat-v1','video_id':video,'url':url,'channel_id':channel,
               'response_sha256':'0'*64,'publishDate':'2020-11-02T00:42:35-08:00','observed_at':'2020-11-02T10:00:00Z'}
        source={'platform':'youtube','url':'https://www.youtube.com/channel/'+channel,'backfill_url':url,'watch_observation':event}
        with patch.object(history,'watch',side_effect=lambda *args:{'published_at':None,'post_url':url}):
            rows,_,_,_=history.collect(source,{})
            self.assertEqual(rows[0]['published_at'],'2020-11-02T08:42:35+00:00')
            with self.assertRaisesRegex(ValueError,'invalid_public_observation'):
                history.collect({**source,'watch_observation':{**event,'channel_id':'UC'+'x'*22}},{})
    def test_public_video_metadata_is_independent_of_playback(self):
        import youtube_history as history
        import json
        video='QVI2ubEKAOA'; channel='UCXoJ8kY9zpLBEz-8saaT3ew'
        source={'platform':'youtube','url':'https://www.youtube.com/channel/'+channel}
        player={'playabilityStatus':{'status':'UNPLAYABLE'},'videoDetails':{'videoId':video,'channelId':channel},
                'microformat':{'playerMicroformatRenderer':{'publishDate':'2026-11-02T00:42:35-08:00'}}}
        embed={'type':'video','title':'Regression fixture','author_url':source['url'],
               'html':'<iframe src="https://www.youtube.com/embed/'+video+'"></iframe>'}
        responses=[(('var ytInitialPlayerResponse = '+json.dumps(player)).encode(),'https://www.youtube.com/watch?v='+video),
                   (json.dumps(embed).encode(),'https://www.youtube.com/oembed')]
        with patch.object(history,'fetch',side_effect=responses):row=history.watch(source,channel,video)
        self.assertEqual(row['published_at'],'2026-11-02T08:42:35+00:00')
    def test_only_stale_primary_rows_trigger_secondary_without_promoting_unknown_dates(self):
        import asyncio
        from unittest.mock import AsyncMock
        from candidate_audit import publication_window,windowed
        import source_adapters
        since=datetime.now(timezone.utc)
        old={'published_at':'2001-11-02T12:00:00+00:00'}
        missing={'published_at':None}
        token=publication_window.set(since)
        try:
            self.assertEqual(windowed([old,missing]),[missing])
            legacy=AsyncMock(return_value=([missing],'ok',[]))
            with patch.object(source_adapters,'primary',return_value='pravaha'):
                items,_,attempts=asyncio.run(source_adapters.collect(None,{'platform':'x','url':'https://x.com/example'},
                          100,False,legacy,None,([old],{'tool':'pravaha','status':'ok','posts':1})))
            self.assertEqual(items,[missing]);self.assertEqual(attempts[0]['outside_window'],1)
            legacy.assert_awaited_once()
        finally:publication_window.reset(token)
    def test_sitemap_uses_publisher_date_and_resumes_the_section_rotation(self):
        from news_sitemap import collect as sitemap
        from crawl_common import CrawlState
        from datetime import timedelta
        base='https://news.example'
        state=CrawlState({'platform':'rss','id':'feed','url':base+'/'},1,3)
        stamp=datetime.now(timezone.utc)
        index='<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join(
            '<sitemap><loc>'+base+'/'+p+'.xml</loc></sitemap>' for p in ('news','a','b'))+'</sitemapindex>'
        def read(url):
            if url.endswith('index.xml'):return index.encode(),url
            xml=('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:n="http://www.google.com/schemas/sitemap-news/0.9">'
                 '<url><loc>'+url+'/article</loc><lastmod>2000-01-01</lastmod><n:news><n:title>Title</n:title>'
                 '<n:publication_date>'+stamp.isoformat()+'</n:publication_date></n:news></url></urlset>')
            return xml.encode(),url
        with patch.object(state,'read',side_effect=read):
            rows,_,_,cursor=sitemap(state,base+'/index.xml',{},stamp-timedelta(days=2))
            more,_,_,cursor=sitemap(state,base+'/index.xml',cursor,stamp-timedelta(days=2))
            last,_,_,_=sitemap(state,base+'/index.xml',cursor,stamp-timedelta(days=2))
        self.assertEqual(len({p['canonical_key'] for p in rows+more+last}),3)
        self.assertTrue(all(p['published_at']==stamp.isoformat() and p['text_may_be_truncated'] for p in rows+more))
    def test_changed_sitemap_is_read_without_skipping_the_full_sweep(self):
        from news_sitemap import collect as sitemap
        from crawl_common import CrawlState
        from datetime import timedelta
        base='https://news.example'
        stamp=datetime.now(timezone.utc)
        urls=[base+'/'+n+'.xml' for n in 'abcde']
        state=CrawlState({'platform':'rss','id':'feed','url':base+'/'},100,4)
        index='<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join(
            '<sitemap><loc>'+u+'</loc><lastmod>2026-01-02</lastmod></sitemap>' for u in urls)+'</sitemapindex>'
        seen=[]
        def read_page(url):
            if url.endswith('/index.xml'):return index.encode(),url
            seen.append(url)
            return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                    'xmlns:n="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>'+url+'/doc</loc>'
                    '<n:news><n:title>Article</n:title><n:publication_date>'+stamp.isoformat()+
                    '</n:publication_date></n:news></url></urlset>').encode(),url
        previous={'sitemap_next':urls[0],'sitemap_sections':{urls[4]:{'modified':'2026-01-01'}}}
        with patch.object(state,'read',side_effect=read_page):
            rows,status,_,cursor=sitemap(state,base+'/index.xml',previous,stamp-timedelta(days=1))
        self.assertEqual((status,len(rows)),('ok',3))
        self.assertEqual(seen,[urls[0],urls[4],urls[1]])
        self.assertEqual(cursor['sitemap_next'],urls[2])

    def test_public_listing_retains_unread_articles_alongside_sitemap(self):
        base='https://news.example'
        source={'platform':'rss','id':'source','url':base+'/',
                'references':[{'kind':'publisher_news_listing','url':base+'/lenta'}]}
        listing=''.join(f'<article><a href="/news/{i}">Publisher headline {i}</a></article>' for i in range(5))
        stamp=datetime.now(timezone.utc).isoformat()
        def read(crawl,url):
            if url.endswith('/lenta'):return listing.encode(),url
            return (f'<meta property="og:type" content="article"><meta property="og:title" content="Title">'
                    f'<meta property="article:published_time" content="{stamp}">').encode(),url
        with patch('crawl_common.CrawlState.read',read),patch('crawl_common.CrawlState.robots_allowed'), \
                patch('news_sitemap.declared',return_value=base+'/news.xml'), \
                patch('news_sitemap.collect',return_value=([],'no_new_posts',[],None)):
            first,_,_,cursor=collect(source,{},max_pages=6)
            self.assertEqual(len(cursor['listing']['pending']),3)
            second,_,_,cursor=collect(source,cursor,max_pages=7)
            self.assertEqual(len({p['canonical_key'] for p in first+second}),5)
            self.assertEqual(cursor['listing']['pending'],[])
            last,_,_,_=collect(source,cursor,max_pages=7)
            self.assertEqual(last,[])

    def test_youtube_public_playlist_partial_owner_metadata_keeps_page_slots(self):
        import youtube_history as history
        from types import SimpleNamespace
        import json
        channel='UC'+'a'*22
        entries=[{'id':'12345678901','channel_id':channel},{'id':'12345678902'},
                 {'id':'12345678903','channel_id':'UC'+'b'*22},{'id':'bad'}]
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'id':'UU'+channel[2:],'channel_id':channel,'entries':entries}))
        with patch.object(history.subprocess,'run',return_value=response):
            self.assertEqual(history.uploads(channel,1),['12345678901','12345678902',None,None])

    def test_youtube_gap_keeps_native_head_and_retries_failed_detail(self):
        import youtube_history as history
        from contextlib import ExitStack
        source={'platform':'youtube','url':'https://www.youtube.com/@tsn','id':'tsn'}
        head=[{'canonical_key':'youtube:'+f'{i:011d}','published_at':datetime.now(timezone.utc).isoformat()} for i in range(15)]
        older=[f'{i:011d}' for i in range(15,21)]
        detail=lambda src,channel,v:{'canonical_key':'youtube:'+v,'published_at':head[0]['published_at']}
        with ExitStack() as context:
            for name,value in [('resolve','UC'+'a'*22),('fetch',(b'','')),('parse',head),
                    ('uploads',[r['canonical_key'][8:] for r in head]+older)]:
                context.enter_context(patch.object(history,name,return_value=value))
            context.enter_context(patch('youtube_history.time.sleep'))
            with patch.object(history,'watch',side_effect=TimeoutError()):
                rows,status,_,cursor=history.collect(source,{})
            self.assertEqual((rows,status),(head,'timeout'))
            self.assertIn(older[0],cursor['pending'])
            with patch.object(history,'watch',side_effect=detail):
                rows,status,_,cursor=history.collect(source,cursor)
                self.assertEqual(len(rows),19)
                rows,status,_,cursor=history.collect(source,cursor)
            self.assertEqual(len(rows),17)
            self.assertFalse(cursor['start'])

    def test_missing_atom_checks_new_uploads_before_resuming_pending_details(self):
        import youtube_history as history
        from urllib.error import HTTPError
        source={'platform':'youtube','url':'https://www.youtube.com/channel/UC'+'a'*22}
        channel='UC'+'a'*22
        prior={'channel':channel,'uploads_head':['old'],'pending':['waiting'],'start':56}
        def detail(src,owner,video):
            return {'canonical_key':'youtube:'+video,'published_at':datetime.now(timezone.utc).isoformat()}
        with patch.object(history,'fetch',side_effect=HTTPError(source['url'],404,'missing',{},None)), patch.object(
                history,'uploads',return_value=['breaking','old']) as listing, patch.object(history,'watch',side_effect=detail), patch.object(history.time,'sleep'):
            rows,_,_,state=history.collect(source,prior,details=1)
            self.assertEqual([r['canonical_key'] for r in rows],['youtube:breaking'])
            self.assertEqual(state['pending'],['waiting'])
            rows,_,_,state=history.collect(source,state,details=1)
            self.assertEqual([r['canonical_key'] for r in rows],['youtube:waiting'])
            self.assertTrue(all(call.args==(channel,1) for call in listing.call_args_list))

    def test_missing_atom_feed_checks_the_verified_uploads_playlist(self):
        import youtube_history as history
        from urllib.error import HTTPError
        source={'platform':'youtube','url':'https://www.youtube.com/channel/UC'+'a'*22,'id':'channel'}
        video='12345678901'
        item={'canonical_key':'youtube:'+video,'published_at':datetime.now(timezone.utc).isoformat()}
        with patch.object(history,'fetch',side_effect=HTTPError(source['url'],404,'missing',{},None)), \
                patch.object(history,'uploads',return_value=[video]),patch.object(history,'watch',return_value=item), \
                patch.object(history.time,'sleep'):
            rows,status,attempts,_=history.collect(source,{})
        self.assertEqual((rows,status),([item],'ok'))
        self.assertEqual(attempts[0]['status'],'http_404')
    def test_verified_permalink_recovery_preserves_the_source_date(self):
        url='https://www.kommersant.ru/doc/9078670'
        source={'url':'https://fixture.invalid/source/39','platform':'rss','backfill_url':url,'id':'publisher'}
        row={'url':url,'text':'Regression fixture','published_at':'2026-11-02T03:30:37+03:00',
             'capture_mode':'public_article_excerpt','media':[]}
        with patch('crawl_common.CrawlState.read',return_value=(b'<html/>',url)),patch('web_pages.article_rows',return_value=[row]):
            posts,status,_,update=collect(source,{})
            self.assertEqual((len(posts),status,update),(1,'ok',None))
            self.assertEqual(posts[0]['published_at'],'2026-11-02T00:30:37+00:00')
        with patch('crawl_common.CrawlState.read',return_value=(b'',source['url'])):
            with self.assertRaisesRegex(ValueError,'unexpected_article_redirect'):collect(source,{})
    def test_rotation_and_platform_backoff(self):
        sources = [{'id':str(i),'url':'https://test.example/'+str(i),'platform':'rss' if i<20 else 'facebook'} for i in range(30)]
        state = {'incremental_polling':{},'polling':{}}
        now = datetime.now(timezone.utc)
        for source in sources[:18]:
            state['incremental_polling'][source['id']] = {'url':source['url'],'next_check_at':'2099-01-01T00:00:00Z'}
        sources[19]['url']='https://limited.example/feed'
        state['polling']['19'] = {'url':sources[19]['url'],'collector_status':'http_429','next_check_at':'2099-01-01T00:00:00Z'}
        batch = due_batch({'sources':sources},state,{'rss','facebook'},now,4)['sources']
        self.assertEqual(batch[0]['id'],'18')
        self.assertEqual([s['platform'] for s in batch[1:]],['facebook']*3)

    def test_feed_reads_beyond_one_hundred_and_resumes_after_archive(self):
        base = 'https://news.example'
        source = {'platform':'rss','id':'feed','url':base+'/1'}
        def read(_,url):
            page = int(url.split('/')[-1])
            entries = ''.join(f'<entry><title>Entry</title><link href="{base}/post-{page}-{i}"/></entry>' for i in range(110))
            link = f'<link rel="next" href="{base}/{page+1}"/>' if page < 4 else ''
            return f'<feed xmlns="http://www.w3.org/2005/Atom">{link}{entries}</feed>'.encode(),url
        with patch('crawl_common.CrawlState.read', read),patch('crawl_common.CrawlState.robots_allowed'):
            rows,status,_,cursor = collect(source,{})
            self.assertEqual((len(rows),status),(330,'ok'))
            self.assertEqual(cursor['pending'],[base+'/4'])
            following,status,_,_ = collect(source,cursor)
        self.assertEqual(len({p['canonical_key'] for p in rows+following}),440)

    def test_listing_links_survive_small_fetch_budgets_and_resume(self):
        base = 'https://news.example'
        source = {'id':'feed','platform':'rss','url':base+'/'}
        links = [f'{base}/news/story-{i}' for i in range(45)]
        listing = ''.join(f'<a href="{link}">Article number {i}</a>' for i,link in enumerate(links))
        def read(crawl,url):
            if url == source['url']:
                return listing.encode(),url
            entry = f'<entry><title>Story</title><link href="{url}"/></entry>'
            return f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'.encode(),url
        cursor = {}
        found = set()
        with patch('crawl_common.CrawlState.read',read), patch('crawl_common.CrawlState.robots_allowed'):
            for _ in range(5):
                rows,status,_,cursor = collect(source,cursor,max_pages=10)
                self.assertEqual(status,'ok')
                found.update(p['post_url'] for p in rows)
        self.assertEqual(found,set(links))

    def test_old_atom_page_retains_next_and_counts_filtered_rows(self):
        base = 'https://news.example'
        source = {'id':'feed','platform':'rss','url':base+'/1'}
        def read(crawl,url):
            link = '<link rel="next" href="/2"/>' if url.endswith('/1') else ''
            date = '2000-01-01T00:00:00Z' if link else datetime.now(timezone.utc).isoformat()
            row = f'<entry><title>Story</title><published>{date}</published><link href="{url}/post"/></entry>'
            return f'<feed xmlns="http://www.w3.org/2005/Atom">{link}{row}</feed>'.encode(),url
        with patch('crawl_common.CrawlState.read',read), patch('crawl_common.CrawlState.robots_allowed'):
            _,_,attempts,cursor = collect(source,{},max_pages=1)
            self.assertEqual(cursor['pending'],[base+'/2'])
            self.assertEqual(attempts[0]['rejected_rows'],{'before_publication_window':1})
            rows,_,_,_ = collect(source,cursor,max_pages=2)
        self.assertEqual([p['post_url'] for p in rows],[base+'/2/post'])
