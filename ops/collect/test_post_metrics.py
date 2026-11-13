import json
import unittest
from unittest.mock import Mock
from engagement_metrics import counters,retain_counters
from post_metrics import tiktok_metrics,youtube_metrics,refresh
from candidate_feeds import tiktok_rows
from short_posts import tiktok_row
from remaining_social import normalize
from media_metadata import normalize as media_rows


class PostMetricsTests(unittest.TestCase):
    def test_slow_optional_metrics_cannot_mutate_or_discard_valid_news(self):
        import asyncio,time
        from unittest.mock import patch
        from post_metrics import bounded_refresh
        items=[{'canonical_key':'telegram:example/1','text':'verified news'}]
        previous={'metrics_offset':3,'metrics_checked_at':'2026-11-01T00:00:00Z'}
        def slow(source,rows,state):
            time.sleep(.04);rows[0]['views']=5
            return {'status':'ok'},{'metrics_offset':4}
        with patch('post_metrics.refresh',side_effect=slow):
            rows,result=asyncio.run(bounded_refresh({},items,previous,seconds=.005))
        self.assertIs(rows,items);self.assertNotIn('views',items[0])
        self.assertEqual(result[0]['status'],'deferred_budget');self.assertEqual(result[1],previous)

    def test_fast_optional_metrics_are_kept_with_their_checkpoint(self):
        import asyncio
        from unittest.mock import patch
        from post_metrics import bounded_refresh
        items=[{'canonical_key':'telegram:example/1','text':'verified news'}]
        def fast(source,rows,state):
            rows[0]['views']=0
            return {'status':'ok'},{'metrics_offset':4}
        with patch('post_metrics.refresh',side_effect=fast):
            rows,result=asyncio.run(bounded_refresh({},items,{},seconds=1))
        self.assertEqual(rows[0]['views'],0);self.assertNotIn('views',items[0])
        self.assertEqual(result[1],{'metrics_offset':4})

    source={'platform':'tiktok','url':'https://www.tiktok.com/@dsns','id':'test'}
    url='https://www.tiktok.com/@dsns/video/123456789'
    def item(self):return {'canonical_key':'tiktok:123456789','post_url':self.url}
    def body(self,owner='dsns',identity='123456789'):
        row={'id':identity,'author':{'uniqueId':owner},'statsV2':{'diggCount':'12','commentCount':'0','shareCount':'3','playCount':'150'}}
        return ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'+json.dumps({'__DEFAULT_SCOPE__':{'webapp.video-detail':{'itemInfo':{'itemStruct':row}}}})+'</script>').encode()

    def test_tiktok_details_use_exact_post_owner_and_preserve_real_zero(self):
        self.assertEqual(tiktok_metrics(self.body(),self.item()),{'likes':12,'comments':0,'shares':3,'views':150})
        self.assertEqual(tiktok_metrics(self.body(owner='other'),self.item()),{})
        self.assertEqual(tiktok_metrics(self.body(identity='999999999'),self.item()),{})
        self.assertEqual(tiktok_metrics(b'<html>unavailable</html>',self.item()),{})

    def test_counters_survive_both_tiktok_adapter_boundaries(self):
        stats={'digg_count':12,'comment_count':0,'share_count':3,'play_count':150}
        rows=tiktok_rows(self.source,[{'url':self.url,'id':'123456789','author':'dsns','desc':'Post','statistics':stats}],5)
        self.assertEqual(counters(rows[0]),{'likes':12,'comments':0,'shares':3,'views':150})
        raw=tiktok_row({'id':'123456789','author':{'uniqueId':'dsns'},'desc':'Post','stats':stats},'dsns',None)
        result=normalize(self.source,[raw],5,'TikTok-Api')
        self.assertEqual(counters(result[0]),counters(rows[0]))

    def test_metadata_adapter_keeps_real_metrics_not_account_or_comment_totals(self):
        row={'id':'123456789','title':'Post','webpage_url':self.url,'like_count':12,'comment_count':0,'repost_count':3,'view_count':150,
             'follower_count':9000,'comments':[{'like_count':8000}]}
        self.assertEqual(counters(media_rows(self.source,row,1)[0]),{'likes':12,'comments':0,'shares':3,'views':150})
        result={};retain_counters(result,{'statsV2':{'diggCount':'-1','shareCount':'1.5','commentCount':True}})
        self.assertEqual(result,{})

    def test_pravaha_retains_replies_and_retweets_but_not_default_zero(self):
        from unittest.mock import patch
        import source_adapters
        tweet={'id':'123','authorHandle':'example','text':'Post','media':[],
               'likes':12,'replies':4,'retweets':3,'views':0}
        with patch.object(source_adapters,'http_json',return_value={'ok':True,'tweets':[tweet]}):
            row=source_adapters.pravaha({'platform':'x','url':'https://x.com/example'},1)[0]
        self.assertEqual(counters(row),{'likes':12,'comments':4,'shares':3,'views':None})

    def test_old_pravaha_defaults_are_removed_on_refresh_without_erasing_native_zero(self):
        from record_quality import merge_observations
        old={'capture_mode':'pravaha','observed_at':'2026-11-05T12:00:00Z','views':0,'likes':0,'engagement':{'views':0,'likes':0}}
        new={'capture_mode':'pravaha','observed_at':'2026-11-05T13:00:00Z','engagement':{'shares':3,'comments':4}}
        self.assertEqual(counters(merge_observations(old,new)),{'views':None,'likes':None,'shares':3,'comments':4})
        old['capture_mode']='x_api'
        self.assertEqual(counters(merge_observations(old,new))['views'],0)

    def test_refresh_throttles_rotates_and_never_erases_known_metrics(self):
        item=self.item();item['engagement']={'likes':7}
        reader=Mock(return_value=(self.body(),self.url))
        event,state=refresh(self.source,[item],{},reader,clock=lambda:2000)
        self.assertEqual(event['status'],'ok');self.assertEqual(item['engagement']['shares'],3)
        self.assertIsNone(refresh(self.source,[item],state,reader,clock=lambda:2100))
        self.assertEqual(reader.call_count,1)
        event,state=refresh(self.source,[item],state,Mock(return_value=(b'<html/>',self.url)),clock=lambda:3000)
        self.assertEqual(event['status'],'not_exposed');self.assertEqual(item['engagement']['likes'],12)

    def test_refresh_stops_on_access_barrier(self):
        reader=Mock(return_value=(b'<html>verify you are human</html>',self.url))
        item=self.item();event,_=refresh(self.source,[item],{},reader,clock=lambda:2000)
        self.assertEqual(event['status'],'challenge_required')
        self.assertNotIn('engagement',item);self.assertEqual(reader.call_count,1)

    def test_youtube_counts_are_scoped_to_current_video_not_recommendations(self):
        video='Abc12345678'
        player={'videoDetails':{'videoId':video,'viewCount':'1534'}}
        data={'currentVideoEndpoint':{'watchEndpoint':{'videoId':video}},
              'contents':{'twoColumnWatchNextResults':{'results':{'results':{'contents':[
                  {'videoPrimaryInfoRenderer':{'videoActions':{'menuRenderer':{'topLevelButtons':[
                      {'segmentedLikeDislikeButtonViewModel':{'likeButtonViewModel':{'accessibilityText':'1,234 likes'},
                                                             'dislikeButtonViewModel':{'accessibilityText':'3 dislikes'}}}]}}}},
                  {'itemSectionRenderer':{'contents':[{'commentsEntryPointHeaderRenderer':{'commentCount':{'simpleText':'0'}}}]}}
              ]}},'secondaryResults':{'like_count':9999}}}}
        body=('var ytInitialPlayerResponse = '+json.dumps(player)+'; var ytInitialData = '+json.dumps(data)+';').encode()
        self.assertEqual(youtube_metrics(body,video),{'views':1534,'likes':1234,'comments':0})
        self.assertEqual(youtube_metrics(body,'OtherId1234'),{})

    def test_youtube_mobile_escaped_json_after_null_player_initializer(self):
        video='6z-EZWrHJ7o'
        data={'currentVideoEndpoint':{'watchEndpoint':{'videoId':video}},'contents':{'singleColumnWatchNextResults':{
            'results':{'results':{'contents':[{'slimVideoMetadataSectionRenderer':{'contents':[
                {'slimVideoActionBarRenderer':{'buttons':[{'buttonViewModel':{'iconName':'LIKE','title':'14'}}]}}
            ]}}]}}}}}
        encoded=json.dumps(data).replace('"',r'\x22').replace('{',r'\x7b').replace('}',r'\x7d')
        body=("var ytInitialPlayerResponse = null; var ytInitialData = '"+encoded+"';var ytInitialPlayerResponse = "+
              json.dumps({'videoDetails':{'videoId':video,'viewCount':'807'}})+';').encode()
        self.assertEqual(youtube_metrics(body,video),{'views':807,'likes':14})
        from youtube_history import player_metadata
        self.assertEqual(player_metadata(body)['videoDetails']['videoId'],video)

if __name__=='__main__':unittest.main()
