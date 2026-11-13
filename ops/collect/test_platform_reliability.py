import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from urllib.error import HTTPError
from threads_public import parse, collect
from source_health import update, instant


class PlatformReliability(unittest.TestCase):
    def test_threads_retains_only_owned_original_public_posts(self):
        source={'id':'th','platform':'threads','url':'https://www.threads.net/@owner','origin':'general'}
        own={'code':'ABC','user':{'username':'owner'},'caption':{'text':'Public post'},'taken_at':1793940000,'like_count':4,
             'video_versions':None,'carousel_media':None,'image_versions2':{'candidates':None}}
        foreign={**own,'code':'OTHER','user':{'username':'someone'}}
        reply={**own,'code':'REPLY','text_post_app_info':{'reply_to_id':'123'}}
        body=('<script type="application/json">'+json.dumps([own,foreign,reply,own])+'</script>').encode()
        rows=parse(source,body,20)
        self.assertEqual([p['canonical_key'] for p in rows],['threads:ABC'])
        self.assertEqual(rows[0]['source_id'],'th')
        self.assertEqual(rows[0]['engagement']['like_count'],4)
        self.assertTrue(rows[0]['published_at'].endswith('+00:00'))
        self.assertEqual(parse({**source,'url':'https://www.threads.com/@owner/post/OTHER'},body,20),[])

    def test_threads_login_is_not_an_empty_success(self):
        source={'platform':'threads','url':'https://www.threads.com/@owner'}
        with patch('run.fetch',return_value=(b'Log in','https://www.threads.com/login')):
            with self.assertRaisesRegex(ValueError,'login_required'):collect(source,20)
        with patch('run.fetch',return_value=(b'<html>Unknown layout</html>',source['url'])):
            self.assertEqual(collect(source,20)[1]['status'],'unverified_markup')

    def test_youtube_drains_pending_instead_of_repeating_slow_listing(self):
        import youtube_history as history
        source={'platform':'youtube','url':'https://www.youtube.com/@owner'}
        channel='UCXoJ8kY9zpLBEz-8saaT3ew'
        now=datetime.now(timezone.utc).isoformat()
        previous={'url':source['url'],'channel':channel,'channel_resolved_at':now,'uploads_head_checked_at':now,
                  'uploads_head':['12345678901'],'pending':['12345678901'],'start':1,'next_start':None}
        item={'canonical_key':'youtube:12345678901','published_at':now}
        with patch.object(history,'fetch',side_effect=HTTPError(source['url'],404,'missing',{},None)), \
             patch.object(history,'uploads',side_effect=AssertionError('head must not starve pending details')), \
             patch.object(history,'watch',return_value=item),patch.object(history.time,'sleep'):
            rows,status,_,state=history.collect(source,previous,reuse_channel=True)
        self.assertEqual((rows,status),([item],'ok'))
        self.assertEqual(state['pending'],[])

    def test_youtube_pending_turn_survives_long_scheduler_pause(self):
        import youtube_history as history
        source={'platform':'youtube','url':'https://www.youtube.com/@owner'}
        now=datetime.now(timezone.utc).isoformat()
        previous={'url':source['url'],'channel':'UCXoJ8kY9zpLBEz-8saaT3ew','channel_resolved_at':now,
                  'uploads_head_checked_at':(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),
                  'drain_pending_next':True,'uploads_head':['12345678901'],
                  'pending':['12345678901'],'start':1,'next_start':None}
        item={'canonical_key':'youtube:12345678901','published_at':now}
        with patch.object(history,'fetch',side_effect=HTTPError(source['url'],404,'missing',{},None)), \
             patch.object(history,'uploads',side_effect=AssertionError('persisted details turn required')), \
             patch.object(history,'watch',return_value=item),patch.object(history.time,'sleep'):
            rows,status,_,state=history.collect(source,previous,reuse_channel=True)
        self.assertEqual((rows,status),([item],'ok'))
        self.assertFalse(state['drain_pending_next'])

    def test_unfinished_youtube_work_has_short_retry_but_retains_server_deadline(self):
        record={'url':'https://www.youtube.com/@owner','platform':'youtube','checked_at':'2026-11-06T07:00:00+00:00',
                'status':'partial_results','new_keys':[],'seconds':25,'remaining_ranges':1,
                'attempts':[{'tool':'youtube_uploads','status':'ok','posts':0}]}
        h=update({},record,target_seconds=300,headroom_seconds=30)
        self.assertEqual((instant(h['next_check_at'])-instant(record['checked_at'])).total_seconds(),60)
        h=update({},dict(record,retry_after='2026-11-06T09:00:00+00:00'),target_seconds=300)
        self.assertEqual(h['next_check_at'],'2026-11-06T09:00:00+00:00')
        self.assertIsNone(h['last_success_at'])

    def test_active_social_interval_adapts_without_changing_telegram_target(self):
        record={'url':'https://www.example.com/owner','platform':'instagram','checked_at':'2026-11-06T07:00:00+00:00',
                'latest_published_at':'2026-11-06T06:59:00+00:00','status':'ok','new_keys':['one'],'seconds':1,'attempts':[]}
        social=update({},record,target_seconds=300,headroom_seconds=30)
        telegram=update({},dict(record,platform='telegram'),target_seconds=300,headroom_seconds=30)
        self.assertEqual((instant(social['next_check_at'])-instant(record['checked_at'])).total_seconds(),60)
        self.assertEqual((instant(telegram['next_check_at'])-instant(record['checked_at'])).total_seconds(),270)
