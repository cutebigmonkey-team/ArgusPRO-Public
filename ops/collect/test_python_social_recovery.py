"""Synthetic upstream failure fixtures; never uses accounts, network or Drive."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch
import python_social_worker as worker


class WorkerRecoveryTests(unittest.TestCase):
    def invoke(self,env):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'result.json'
            with patch.dict(os.environ,env,clear=True),patch('sys.platform','linux'),patch('sys.argv',['worker','instaloader','https://www.instagram.com/example','2',str(target)]),patch.object(worker,'instagram',return_value=[{'url':'public-fixture'}]) as collect:
                worker.main()
            return json.loads(target.read_text()),collect

    def test_explicit_hostless_uses_same_cloud_guard_without_github_impersonation(self):
        result,collect=self.invoke({'ARGUS_COLLECTOR_RUNTIME':'hostless'})
        self.assertEqual(result['status'],'ok');collect.assert_called_once()

    def test_local_runtime_is_denied_before_adapter_execution(self):
        with self.assertRaises(RuntimeError):self.invoke({})

    def test_non_linux_hostless_is_denied(self):
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','win32'),patch.object(worker,'instagram') as collect:
            with self.assertRaises(RuntimeError):worker.main()
            collect.assert_not_called()

    def test_github_hosted_remains_supported(self):
        result,_=self.invoke({'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted'})
        self.assertEqual(result['status'],'ok')

    def loader_module(self,profile):
        loader=Mock()
        module=SimpleNamespace(Instaloader=Mock(return_value=loader),Profile=SimpleNamespace(from_username=profile))
        return loader,module

    def test_failed_initial_profile_lookup_closes_loader(self):
        loader,module=self.loader_module(Mock(side_effect=TimeoutError('secret upstream text')))
        with patch.dict('sys.modules',{'instaloader':module}):
            with self.assertRaises(TimeoutError):worker.instagram('https://www.instagram.com/example',2)
        loader.close.assert_called_once()

    def test_instagram_partial_iterator_retains_first_publication_with_error_status(self):
        def posts():
            yield SimpleNamespace(shortcode='ABC',owner_username='example',caption='fixture',date_utc=datetime(2026,11,13),url='https://example.test/media')
            raise TimeoutError('SECRET token URL must not be serialized')
        profile=SimpleNamespace(is_private=False,get_posts=posts)
        loader,module=self.loader_module(Mock(return_value=profile))
        with tempfile.TemporaryDirectory() as directory,patch.dict('sys.modules',{'instaloader':module}),patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','linux'):
            target=Path(directory)/'result.json'
            with patch('sys.argv',['worker','instaloader','https://www.instagram.com/example','2',str(target)]):worker.main()
            result=json.loads(target.read_text())
        self.assertEqual(result['status'],'partial_results')
        self.assertEqual(result['stop_reason'],'timeout')
        self.assertEqual(result['rows'][0]['url'],'https://www.instagram.com/p/ABC/')
        self.assertNotIn('SECRET',json.dumps(result));loader.close.assert_called_once()

    def test_facebook_partial_iterator_retains_publication(self):
        def posts(**kwargs):
            yield {'post_url':'https://www.facebook.com/example/posts/123','text':'fixture','username':'example','time':datetime(2026,11,13,tzinfo=timezone.utc)}
            raise TimeoutError('SECRET')
        with tempfile.TemporaryDirectory() as directory,patch.dict('sys.modules',{'facebook_scraper':SimpleNamespace(get_posts=posts)}),patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','linux'):
            target=Path(directory)/'result.json'
            with patch('sys.argv',['worker','facebook-scraper','https://www.facebook.com/example','2',str(target)]):worker.main()
            result=json.loads(target.read_text())
        self.assertEqual(result['status'],'partial_results');self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['stop_reason'],'timeout');self.assertNotIn('SECRET',json.dumps(result))

    def test_parent_keeps_partial_status_and_normalizes_rows(self):
        import cloud_social
        source={'platform':'instagram','url':'https://www.instagram.com/example'}
        async def process(command,root,output,*args,**kwargs):
            output.write_text(json.dumps({'status':'partial_results','rows':[{'url':'https://www.instagram.com/p/ABC/','owner':'example','text':'fixture'}],'stop_reason':'timeout'}))
            return 0,None,root/'unused'
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','linux'),patch.object(cloud_social,'process',side_effect=process):
            rows,attempt=asyncio.run(cloud_social.library(source,2))
        self.assertEqual(rows[0]['canonical_key'],'instagram:ABC')
        self.assertEqual(attempt['status'],'partial_results')
        self.assertEqual(attempt['stop_reason'],'timeout')


class PartialRestrictionTests(unittest.TestCase):
    def test_worker_retains_rows_without_downgrading_rate_limit(self):
        row={'url':'https://www.instagram.com/p/ABC/','owner':'example','text':'fixture'}
        error=worker.PartialCollection([row],ValueError('429 SECRET'))
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','linux'),patch.object(worker,'instagram',side_effect=error):
            output=Path(directory)/'result.json'
            with patch('sys.argv',['worker','instaloader','https://www.instagram.com/example','2',str(output)]):worker.main()
            result=json.loads(output.read_text())
        self.assertEqual(result['status'],'http_429');self.assertEqual(result['rows'],[row])
        self.assertFalse(result['fetch_complete']);self.assertNotIn('SECRET',json.dumps(result))

    def test_parent_preserves_restriction_with_valid_rows(self):
        import cloud_social
        source={'platform':'instagram','url':'https://www.instagram.com/example'}
        async def process(command,root,output,*args,**kwargs):
            output.write_text(json.dumps({'status':'http_403','fetch_complete':False,'rows':[{'url':'https://www.instagram.com/p/ABC/','owner':'example','text':'fixture'}],'stop_reason':'http_403'}))
            return 0,None,root/'unused'
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True),patch('sys.platform','linux'),patch.object(cloud_social,'process',side_effect=process):
            rows,attempt=asyncio.run(cloud_social.library(source,2))
        self.assertEqual(len(rows),1);self.assertEqual(attempt['status'],'http_403')
        self.assertFalse(attempt['fetch_complete'])
