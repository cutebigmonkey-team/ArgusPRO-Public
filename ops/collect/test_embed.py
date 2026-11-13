import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import embed_social as social
from embed_worker import instagram_rows, facebook_rows, gallery_row, php_parse, gallery, embed_diagnostics


def source(platform='instagram'):
    return {'id':'fixture','platform':platform,'url':'https://www.'+platform+'.com/example'}


def instagram_fixture():
    context = {'context':{'username':'example','full_name':'Example','owner_id':'7','posts_count':1,
        'graphql_media':[{'shortcode_media':{'__typename':'GraphImage','id':'42','shortcode':'ABCDE',
        'owner':{'username':'example'},'taken_at_timestamp':1793318400,'dimensions':{'width':100,'height':100},
        'edge_media_to_caption':{'edges':[{'node':{'text':'Український допис'}}]}}}]}}
    return '<script>'+json.dumps({'contextJSON':json.dumps(context)},separators=(',',':'))+'</script>'


class OwnershipTests(unittest.TestCase):
    def test_embed_diagnostics_distinguish_missing_payload_and_foreign_profile(self):
        details = embed_diagnostics(source(),{'profile':None},'<script>{"contextJSON":null}</script>')
        self.assertTrue(details['context_null'])
        self.assertFalse(details['profile_parsed'])
        details = embed_diagnostics(source(),{'profile':{'user':'foreign','posts':[]}},instagram_fixture())
        self.assertTrue(details['profile_parsed'])
        self.assertFalse(details['profile_matches'])

    def test_foreign_embed_and_post_owner_are_rejected(self):
        post = {'code':'ABCDE','caption':'post','owner':'foreign'}
        self.assertEqual(instagram_rows(source(),{'profile':{'user':'foreign','posts':[post]}}),[])
        self.assertEqual(instagram_rows(source(),{'profile':{'user':'example','posts':[post]}}),[])

    def test_collaboration_requires_explicit_profile_membership(self):
        post = {'code':'ABCDE','caption':'post','owner':'foreign','coauthors':['example']}
        rows = instagram_rows(source(),{'profile':{'user':'example','posts':[post]}})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source_owner'],'example')

    def test_facebook_suggested_post_and_profile_metadata_not_imported(self):
        data = {'canonical':source('facebook')['url'],'items':[
            {'uri':'https://www.facebook.com/foreign/posts/42','content':'other','uid':'42'},
            {'uri':source('facebook')['url'],'title':'profile','content':'','enclosures':[]}]}
        rows = facebook_rows(source('facebook'),data)
        got = social.normalize(source('facebook'),rows,5,'rss-bridge','test')
        self.assertEqual(got,[])

    def test_facebook_numeric_post_needs_matching_actor(self):
        data = {'items':[{'uri':'https://www.facebook.com/42','uid':'42','content':'Post',
                         'actors':[{'url':'https://www.facebook.com/example','id':'7'}]}]}
        rows = facebook_rows(source('facebook'),data)
        got = social.normalize(source('facebook'),rows,5,'rss-bridge','test')
        self.assertEqual(got[0]['canonical_key'],'facebook:42')
        data['items'][0]['actors'][0]['url'] = 'https://foreign.example/example'
        self.assertEqual(facebook_rows(source('facebook'),data),[])
        data['items'][0]['actors'][0]['url'] = 'https://www.facebook.com/foreign'
        self.assertEqual(facebook_rows(source('facebook'),data),[])

    def test_gallery_owner_and_post_identity(self):
        metadata = {'username':'foreign','post_url':'https://www.instagram.com/p/ABCDE/','description':'x'}
        self.assertIsNone(gallery_row(source(),metadata,'https://cdn.example/a.jpg',set()))
        metadata = {'id':'42','user_id':'8','caption':'x'}
        self.assertIsNone(gallery_row(source('facebook'),metadata,'https://cdn.example/a.jpg',{'7'}))
        metadata['user_id'] = '7'
        row = gallery_row(source('facebook'),metadata,'https://cdn.example/a.jpg',{'7'})
        self.assertEqual(social.normalize(source('facebook'),[row],5,'gallery-dl','test')[0]['canonical_key'],'facebook:42')

    def test_cloud_only(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'},clear=True):
            with self.assertRaises(RuntimeError): asyncio.run(social.collect(source(),5))

    def test_explicit_hostless_linux_is_allowed_but_unmarked_local_is_not(self):
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','linux'):
            social.require_cloud()
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','win32'):
            with self.assertRaises(RuntimeError):social.require_cloud()

    def test_block_stops_other_engines(self):
        with patch.object(social,'require_cloud'),patch.object(social,'adapter',new_callable=AsyncMock) as call:
            call.return_value = [],social.diagnostic('instagramprofiles','http_429',[])
            _,attempts = asyncio.run(social.collect(source(),5))
            self.assertEqual(len(attempts),1)
            call.assert_awaited_once()

    def test_run_pipeline_respects_new_engine_block(self):
        import run
        with patch.dict(os.environ,{'ARGUS_EMBED_COLLECTION':'1'}),patch.object(social,'collect',new_callable=AsyncMock) as call,patch.object(run,'social',new_callable=AsyncMock) as browser:
            call.return_value = [],[social.diagnostic('rss-bridge','http_429',[])]
            _,status,_ = asyncio.run(run.collect_source(None,source('facebook'),5))
            self.assertEqual(status,'http_429'); browser.assert_not_called()


class PinnedParserTests(unittest.TestCase):
    def setUp(self):
        self.tools = social.tool_root()

    def test_actual_instagramprofiles_parser(self):
        if not (self.tools/'instagramprofiles/lib/InstagramProfilesPage.php').exists():
            self.skipTest('Pinned PHP libraries are installed on hosted CI')
        with tempfile.TemporaryDirectory() as directory:
            data = php_parse('instagramprofiles',self.tools,instagram_fixture(),Path(directory))
        rows = instagram_rows(source(),data)
        items = social.normalize(source(),rows,5,'instagramprofiles','test')
        self.assertEqual(items[0]['text'],'Український допис')
        self.assertEqual(items[0]['canonical_key'],'instagram:ABCDE')
        self.assertEqual(items[0]['media_urls'],['https://www.instagram.com/p/ABCDE/media/?size=l'])

    def test_actual_rss_bridge_embedded_timeline_parser(self):
        if not (self.tools/'rss-bridge/bridges/FacebookBridge.php').exists():
            self.skipTest('Pinned PHP libraries are installed on hosted CI')
        node = {'post_id':'42','creation_time':1793318400,'comet_sections':{'content':{'story':{
            'message':{'text':'Український допис'},'wwwURL':'https://www.facebook.com/example/posts/42',
            'actors':[{'id':'7','url':'https://www.facebook.com/example'}]}}},
            'attachments':[{'photo_image':{'uri':'https://cdn.example/photo.jpg'}}]}
        body = '<script type="application/json">'+json.dumps({'timeline_list_feed_units':{'edges':[{'node':node}]}},ensure_ascii=False)+'</script>'
        with tempfile.TemporaryDirectory() as directory:
            data = php_parse('rss-bridge',self.tools,body,Path(directory))
        rows = facebook_rows(source('facebook'),data)
        items = social.normalize(source('facebook'),rows,5,'rss-bridge','test')
        self.assertEqual(items[0]['text'],'Український допис')
        self.assertEqual(items[0]['media_urls'],['https://cdn.example/photo.jpg'])

    def test_actual_gallery_and_web_modules_no_media_download(self):
        if not (self.tools/'gallery-dl-web/backend/src').exists():
            self.skipTest('Pinned gallery libraries are installed in hosted CI')
        try:
            import gallery_dl
        except ImportError:
            self.skipTest('Isolated gallery environment required')
        photo = '{"__isNode":"Photo","id":"42","owner":{"__typename":"User","id":"7"},"created_time":1793318400,"image":{"uri":"https://cdn.example/image.jpg","width":100}}'
        class OfflineHTTP:
            urls = []
            def request(self,url,*args,**kwargs):
                self.urls.append(url)
                if '/media/set/' in url: return '{}'
                return photo
        http = OfflineHTTP()
        s = {**source('facebook'),'url':'https://www.facebook.com/photo/?fbid=42'}
        with tempfile.TemporaryDirectory() as directory:
            rows,reason = gallery(s,self.tools,http,Path(directory))
        self.assertIsNone(reason)
        items = social.normalize(s,rows,5,'gallery-dl','test')
        self.assertEqual(items[0]['canonical_key'],'facebook:42')
        self.assertEqual(items[0]['media_urls'],['https://cdn.example/image.jpg'])
        self.assertTrue(all(url.startswith('https://www.facebook.com/') for url in http.urls))

    def test_actual_gallery_web_anonymous_settings(self):
        if not (self.tools/'gallery-dl-web/backend/src').exists():
            self.skipTest('Pinned wrapper modules are installed on hosted CI')
        sys.path.insert(0,str(self.tools/'gallery-dl-web/backend/src'))
        from gallery_dl_web.gallerydl.config_builder import apply
        class Recorder:
            values = {}
            def set(self,path,key,value): self.values[(path,key)] = value
        config = Recorder()
        apply({'platform':'instagram','output_dir':'unused','anonymous':True,'cookies':None,
               'options':{'include':'posts,stories,saved,followers'}},config)
        self.assertEqual(config.values[(('extractor','instagram'),'include')],'posts')
        self.assertFalse(config.values[(('extractor',),'cookies-update')])


if __name__=='__main__': unittest.main()
