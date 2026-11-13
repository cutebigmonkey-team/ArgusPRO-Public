import asyncio
import os
import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import cloud_social as social


class SocialTests(unittest.TestCase):
    source = {'platform':'threads','url':'https://www.threads.net/@example','id':'source'}

    def test_identity_ownership_and_deduplication(self):
        row={'permalink':'https://www.threads.com/@example/post/ABC?tracking=yes','text':'Actual post',
             'username':'example','timestamp':'2026-10-30T10:00:00Z','like_count':None}
        bad=[{**row,'permalink':'https://evil.example/@example/post/ABC'},
             {**row,'permalink':'https://www.threads.com/@other/post/ABC'},
             {**row,'permalink':'https://www.threads.com/@example'},
             {**row,'username':'someone_else'}]
        self.assertEqual(social.threads_rows(self.source,bad,5,'threads-agent'),[])
        result=social.threads_rows(self.source,[row,row],5,'threads-agent')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['canonical_key'],'threads:ABC')
        self.assertIsNone(result[0]['engagement']['like_count'])
        self.assertNotIn('?',result[0]['post_url'])

    def test_single_post_does_not_accept_recommendations(self):
        source={**self.source,'url':self.source['url']+'/post/ABC'}
        rows=[{'post_url':'https://www.threads.com/@example/post/XYZ','text':'Other post'}]
        self.assertEqual(social.threads_rows(source,rows,5,'threads-crawler'),[])

    def test_telegram_preserves_media_only_and_filters_foreign_channels(self):
        source={'platform':'telegram','url':'https://t.me/example'}
        body=b'''<div class="tgme_widget_message" data-post="example/42"><div class="tgme_widget_message_text">First<br>Second</div></div>
        <div class="tgme_widget_message" data-post="example/43"><video src="https://cdn.example/video.mp4"></video></div>'''
        parser=lambda *args:SimpleNamespace(messages=[SimpleNamespace(channel_username='example',tg_msg_id=42,
            text='First Second',posted_at=None,views=12,mentions=['other'],forward_from=None),
            SimpleNamespace(channel_username='foreign',tg_msg_id=44)])
        items=social.telegram_rows(source,5,body,parser)
        self.assertEqual([p['canonical_key'] for p in items],['telegram:example/42','telegram:example/43'])
        self.assertEqual(items[0]['text'],'First\nSecond')
        self.assertEqual(items[0]['views'],12)
        self.assertEqual(items[1]['media_urls'],['https://cdn.example/video.mp4'])

    def test_real_radar_parser_when_installed(self):
        try:
            from tg_radar.parser import parse_tme_page
        except ImportError:
            self.skipTest('Pinned upstream parser runs in cloud CI')
        source={'platform':'telegram','url':'https://t.me/example'}
        body=b'<div class="tgme_widget_message" data-post="example/42"><div class="tgme_widget_message_text">Actual post</div><span class="tgme_widget_message_views">1.2K</span><time datetime="2026-10-30T10:00:00Z"></time></div>'
        items=social.telegram_rows(source,5,body,parse_tme_page)
        self.assertEqual(items[0]['views'],1200)
        self.assertEqual(items[0]['canonical_key'],'telegram:example/42')

    def test_access_blocks_stop_fallback(self):
        for status in social.STOP:
            mock=AsyncMock(return_value=([],social.diagnostic('threads-agent',status,[])))
            with patch.object(social,'threads',mock):
                _,attempts=asyncio.run(social.collect(self.source,5))
            self.assertEqual(len(attempts),1)
            self.assertEqual(mock.await_count,1)

    def test_empty_primary_tries_browser_and_returns_its_status(self):
        mock=AsyncMock(side_effect=[([],social.diagnostic('threads-agent','no_public_posts',[])),
                                   ([],social.diagnostic('threads-crawler','login_required',[]))])
        with patch.object(social,'threads',mock):
            _,attempts=asyncio.run(social.collect(self.source,5))
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[-1]['status'],'login_required')

    def test_local_collection_rejected(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'GitHub-hosted'):
                asyncio.run(social.threads(self.source,5))

    def test_freshrss_parser_when_installed(self):
        target=social.tool_root()/'telegramchannels'
        if not target.exists():self.skipTest('Pinned PHP parser runs in cloud CI')
        request={'channel':'example','html':'<meta charset="utf-8"><div class="tgme_widget_message" data-post="example/42"><div class="tgme_widget_message_text">Actual publication</div><a class="tgme_widget_message_date" href="https://t.me/example/42"><time datetime="2026-10-30T10:00:00Z"></time></a></div>'}
        result=subprocess.run(['php',str(Path(social.__file__).with_name('telegramchannels_runner.php')),str(target)],
            input=json.dumps(request),text=True,capture_output=True,timeout=8,check=True)
        items=social.telegram_enrich({'platform':'telegram','url':'https://t.me/example'},[],json.loads(result.stdout),5)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['canonical_key'],'telegram:example/42')

    def test_library_owner_and_direct_permalink_validation(self):
        source={'platform':'instagram','url':'https://www.instagram.com/example/'}
        row={'url':'https://www.instagram.com/p/ABC/','owner':'other','text':'Post'}
        self.assertEqual(social.library_rows(source,[row],5,'instaloader'),[])
        row['owner']='example'
        self.assertEqual(social.library_rows(source,[row],5,'instaloader')[0]['canonical_key'],'instagram:ABC')
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        row={'url':'https://www.facebook.com/other/posts/42','text':'Recommendation','owner':'other'}
        self.assertEqual(social.library_rows(source,[row],5,'facebook-scraper'),[])

    def test_media_adapters_require_exact_video_identity(self):
        from media_metadata import eligible,normalize
        for platform,url,other,key in (
            ('vk','https://vk.com/video-12_34','https://vk.com/video-12_35','vk:video-12_34'),
            ('ok','https://ok.ru/video/123','https://ok.ru/video/456','ok:video123')):
            source={'platform':platform,'url':url}
            self.assertTrue(eligible(source))
            self.assertFalse(eligible({'platform':platform,'url':url.split('/video')[0]+'/profile'}))
            row={'id':'123','title':'Video','webpage_url':url}
            self.assertEqual(normalize(source,row,5)[0]['canonical_key'],key)
            self.assertEqual(normalize(source,{**row,'webpage_url':other},5),[])

    def test_pipeline_routes_to_upstream(self):
        import run
        mock=AsyncMock(return_value=([],[social.diagnostic('threads-agent','login_required',[])]))
        with patch.dict(os.environ,{'ARGUS_SOCIAL_COLLECTION':'1'}),patch.object(social,'collect',mock):
            _,status,attempts=asyncio.run(run.collect_source(None,self.source,5))
        self.assertEqual(status,'login_required')
        self.assertEqual(attempts[0]['tool'],'threads-agent')

    @unittest.skipUnless(os.name=='posix','Linux process groups run in cloud CI')
    def test_real_worker_deadline_and_output_bounds(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted'}):
            root=Path(temp)
            code,status,_=asyncio.run(social.process([sys.executable,'-c','import time; time.sleep(10)'],root,root/'result',.3))
            self.assertEqual(status,'timeout')
            self.assertNotEqual(code,0)
            code,status,_=asyncio.run(social.process([sys.executable,'-c','print("x"*(9*1024**2))'],root,root/'result',5))
            self.assertEqual(status,'output_limit')

    @unittest.skipUnless(os.name=='posix','Linux browser worker runs in cloud CI')
    def test_browser_launches_inside_sanitized_worker_environment(self):
        script=social.tool_root()/'threads-crawler/argus-smoke.mjs'
        if not script.exists():self.skipTest('Browser toolchain installed in cloud CI')
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted'}):
            root=Path(temp)
            code,status,stdout=asyncio.run(social.process(['node',str(script)],root,root/'result',25))
            self.assertIsNone(status)
            self.assertEqual(code,0,(root/'stderr').read_text())
            self.assertIn('Ukrainian fixture passed',stdout.read_text())
