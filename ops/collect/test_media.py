import asyncio,hashlib,os,unittest
from unittest.mock import patch
from media_metadata import normalize,failure,extract

class MediaTests(unittest.TestCase):
    def test_playlist_is_not_a_post_and_video_identity_matches_feed(self):
        source={'platform':'youtube','url':'https://www.youtube.com/@example'}
        self.assertEqual(normalize(source,{'id':'UCchannel','title':'Channel'},5),[])
        item={'id':'Abc12345678','title':'Video','description':'Real description','url':'https://www.youtube.com/watch?v=Abc12345678','http_headers':{'Authorization':'private'}}
        result=normalize(source,{'entries':[item,item]},5)
        self.assertEqual(len(result),1);self.assertEqual(result[0]['canonical_key'],'youtube:Abc12345678')
        self.assertNotIn('http_headers',result[0]);self.assertIsNone(result[0]['published_at'])
        self.assertEqual(normalize(source,{**item,'url':'https://attacker.example/post'},5),[])

    def test_rutube_live_identity_matches_existing_archive(self):
        identity='a'*32;url='https://rutube.ru/live/video/'+identity
        result=normalize({'platform':'rutube','url':url},{'id':identity,'title':'Live','webpage_url':'https://rutube.ru/video/'+identity+'/'},1)
        canonical='https://rutube.ru/video/'+identity+'/'
        self.assertEqual(result[0]['canonical_key'],'rutube:'+hashlib.sha256(canonical.encode()).hexdigest())

    def test_local_execution_rejected_and_errors_sanitized(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'GitHub-hosted'):asyncio.run(extract({'platform':'youtube'}))
        self.assertEqual(failure('ERROR: Sign in to confirm you are not a bot; cookie=secret'),'login_required')
        self.assertEqual(failure('ERROR: private_token=secret'),'extraction_failed')

    def test_fallback_preserves_primary_error_and_does_not_retry_rate_limit(self):
        from unittest.mock import AsyncMock
        import run
        source={'platform':'youtube','url':'https://www.youtube.com/@example'}
        fallback=AsyncMock(return_value=([],{'tool':'yt-dlp','status':'unavailable','posts':0}))
        with patch.object(run,'feeds',side_effect=ValueError('channel_id_unresolved')),patch('media_metadata.extract',fallback):
            items,status,attempts=asyncio.run(run.collect_source(None,source,5,True))
        self.assertEqual(status,'channel_id_unresolved');self.assertEqual(len(attempts),2)
        fallback.reset_mock()
        with patch.object(run,'feeds',side_effect=ValueError('http_429')),patch('media_metadata.extract',fallback):
            items,status,attempts=asyncio.run(run.collect_source(None,source,5,True))
        fallback.assert_not_called();self.assertEqual(status,'http_429')

    def test_successful_fallback_uses_same_post_schema(self):
        from unittest.mock import AsyncMock
        import run
        source={'platform':'youtube','url':'https://www.youtube.com/@example'}
        expected=normalize(source,{'id':'Abc12345678','title':'Real video'},1)
        with patch.object(run,'feeds',return_value=[]),patch('media_metadata.extract',AsyncMock(return_value=(expected,{'tool':'yt-dlp','status':'ok','posts':1}))):
            items,status,attempts=asyncio.run(run.collect_source(None,source,5,True))
        self.assertEqual(status,'ok');self.assertEqual(items,expected);self.assertEqual(attempts[0]['status'],'no_public_posts')
