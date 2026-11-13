import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comment_adapters import collect, wordpress
from comment_official import facebook, instagram, threads, x, tiktok, vk


class OfficialComments(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_facebook_stream_preserves_parent_and_only_cursor(self):
        post = dict(platform='facebook', platform_post_id='12_34')
        data = dict(data=[dict(id='5', message='Reply', parent={'id':'4'})],
            paging={'next':'https://evil.invalid/?access_token=secret', 'cursors':{'after':'next'}})
        with patch.dict(os.environ, {'ARGUS_FACEBOOK_COMMENTS_TOKEN':'secret', 'ARGUS_META_API_VERSION':'v25.0'}), patch('comment_official.api', return_value=data) as api:
            answer = facebook(post, None)
        self.assertEqual(answer['rows'][0]['parent_id'], '4')
        self.assertEqual(answer['cursor']['page'], 'next')
        self.assertNotIn('secret', json.dumps(answer))
        self.assertIn('filter=stream', api.call_args.args[0])

    def test_instagram_root_reply_pages_resume_after_restart(self):
        post = dict(platform='instagram', platform_post_id='123')
        pages = [dict(data=[dict(id='1', text='Root')], paging={'next':'unused','cursors':{'after':'roots2'}}),
            dict(data=[dict(id='2', text='Reply')], paging={'next':'unused','cursors':{'after':'replies2'}}),
            dict(data=[]), dict(data=[])]
        with patch.dict(os.environ, {'ARGUS_INSTAGRAM_COMMENTS_TOKEN':'token','ARGUS_META_API_VERSION':'v25.0'}), patch('comment_official.api', side_effect=pages) as api:
            first=instagram(post,None)
            second=instagram(post,json.loads(json.dumps(first['cursor'])))
            self.assertEqual(second['rows'][0]['parent_id'],'1')
            third=instagram(post,second['cursor'])
            self.assertEqual(third['cursor']['page'],'roots2')
            self.assertNotIn('reply_page',third['cursor'])
            final=instagram(post,third['cursor'])
            self.assertEqual(final['status'],'success')
            self.assertIn('/1/replies?',api.call_args_list[1].args[0])
            self.assertIn('after=roots2',api.call_args_list[3].args[0])

    def test_threads_identity_required_and_conversation_parents(self):
        with patch.dict(os.environ, {'ARGUS_THREADS_COMMENTS_TOKEN':'token'}), patch('comment_official.api') as api:
            self.assertEqual(threads({},None)['reason'],'verified_platform_post_id_required')
            api.assert_not_called()
            api.return_value={'data':[{'id':'2','text':'Reply','replied_to':{'id':'1'}}]}
            answer=threads({'platform_post_id':'1'},None)
            self.assertIsNone(answer['rows'][0]['parent_id'])

    def test_x_rejects_siblings_and_marks_recent_window_partial(self):
        post=dict(url='https://x.com/example/status/123')
        data={'data':[dict(id='1',text='Reply',conversation_id='123',referenced_posts=[dict(type='replied_to',id='123')]),
                      dict(id='2',text='Unrelated',conversation_id='999',referenced_tweets=[dict(type='replied_to',id='999')])],
              'meta':{'result_count':2,'next_token':'page2'}}
        with patch.dict(os.environ, {'ARGUS_X_BEARER_TOKEN':'token'}), patch('comment_official.api', return_value=data):
            answer=x(post,None)
            self.assertEqual([r['id'] for r in answer['rows']],['1'])
            self.assertEqual(answer['status'],'partial')
            self.assertEqual(answer['cursor'],{'page':'page2'})
            data['errors']=[{'title':'Incomplete response'}]
            incomplete=x(post,None)
            self.assertEqual(incomplete['status'],'failed')
            self.assertEqual(len(incomplete['rows']),1)
            self.assertIsNone(incomplete['cursor'])
            self.assertEqual(x({**post,'conversation_id':'999'},None)['reason'],'conversation_root_required')

    def test_tiktok_short_page_uses_has_more_and_fetches_replies(self):
        post=dict(url='https://www.tiktok.com/@example/video/123')
        pages=[{'data':{'comments':[{'id':1,'text':'Top','reply_count':1}], 'has_more':True,'cursor':5}},
               {'data':{'comments':[{'id':2,'text':'Reply','parent_comment_id':1}], 'has_more':False}},
               {'data':{'comments':[], 'has_more':False}}]
        with patch.dict(os.environ, {'ARGUS_TIKTOK_RESEARCH_TOKEN':'token'}), patch('comment_official.api', side_effect=pages) as api:
            first=tiktok(post,None)
            second=tiktok(post,first['cursor'])
            self.assertEqual(second['rows'][0]['parent_id'],'1')
            final=tiktok(post,second['cursor'])
            self.assertEqual(final['status'],'success')
            self.assertEqual(api.call_args_list[1].args[2]['comment_id'],1)
            self.assertEqual(api.call_args_list[2].args[2]['cursor'],5)

    def test_vk_root_and_reply_pagination(self):
        pages=[{'response':{'items':[dict(id=1,text='Top',thread={'count':1})],'count':1}},
               {'response':{'items':[dict(id=2,text='Reply',reply_to_comment=1)],'count':1}}]
        with patch.dict(os.environ, {'ARGUS_VK_COMMENTS_TOKEN':'token'}), patch('comment_adapters.get', side_effect=pages):
            first=vk(dict(url='https://vk.com/wall-1_2'),None)
            second=vk(dict(url='https://vk.com/wall-1_2'),first['cursor'])
            self.assertEqual(second['status'],'success')
            self.assertEqual(second['rows'][0]['parent_id'],'-1_1')
            self.assertEqual(first['rows'][0]['id'],'-1_1')

    def test_api_access_errors_are_not_empty_success(self):
        with patch.dict(os.environ, {'ARGUS_TIKTOK_RESEARCH_TOKEN':'token'}), patch('comment_official.api',return_value={'error':{'code':'scope_not_authorized','message':'sensitive'}}):
            answer=tiktok(dict(url='https://www.tiktok.com/@example/video/123'),None)
        self.assertEqual(answer['status'],'forbidden')
        self.assertNotIn('sensitive',json.dumps(answer))

    def test_x_limit_reset_and_http_date_retry(self):
        error=urllib.error.HTTPError('redacted',429,'limit',{'Retry-After':'Thu, 01 Jan 1970 00:05:00 GMT','x-rate-limit-reset':'400'},None)
        with patch('comment_adapters.ADAPTERS',{'x':lambda *a:(_ for _ in ()).throw(error)}),patch('comment_adapters.time.time',return_value=100):
            answer=collect(dict(platform='x',url='https://x.com/example/status/123'))
        self.assertEqual(answer['retry_after'],300)
        self.assertEqual(answer['status'],'rate_limited')

    def test_wordpress_exact_page_multiple_finishes(self):
        error=urllib.error.HTTPError('redacted',400,'page',{},io.BytesIO(b'{"code":"rest_comment_invalid_page_number"}'))
        with patch('crawl_common.CrawlState.read',side_effect=error):
            answer=wordpress(dict(url='https://example.org/post'),dict(endpoint='https://example.org/wp-json/wp/v2/comments',post='1',page=2))
        self.assertEqual(answer['status'],'success')

    def test_meta_http_token_error_is_auth_required_and_youtube_quota_pauses(self):
        for platform,url,payload,expected in [
            ('facebook','https://www.facebook.com/test/posts/1',{'error':{'code':190}},'auth_required'),
            ('youtube','https://youtu.be/abcdefghijk',{'error':{'code':403,'errors':[{'reason':'quotaExceeded'}]}},'rate_limited')]:
            error=urllib.error.HTTPError('redacted',400,'api',{},io.BytesIO(json.dumps(payload).encode()))
            with patch('comment_adapters.ADAPTERS',{platform:lambda *a:(_ for _ in ()).throw(error)}):
                answer=collect(dict(platform=platform,url=url))
            self.assertEqual(answer['status'],expected)

    def test_registered_platforms_have_distinct_adapters(self):
        from comment_adapters import ADAPTERS
        names=('telegram','facebook','instagram','x','threads','tiktok','youtube','vk','ok','rss')
        self.assertEqual(len({ADAPTERS[p] for p in names}),len(names))


if __name__=='__main__':unittest.main()
