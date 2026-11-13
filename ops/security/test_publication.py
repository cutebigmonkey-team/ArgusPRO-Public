import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'collect'),str(Path(__file__).resolve().parent)]
from publication_audit import inspect,inspect_payload
from import_sheet import source_sheet_url

class PublicationTests(unittest.TestCase):
    def test_secret_detector_never_exports_values(self):
        value='ghp_'+'Q7w9AbCd'*5
        result=inspect(('token='+value).encode())
        self.assertEqual(result[0]['category'],'github_token_candidate')
        self.assertNotIn(value,json.dumps(result))

    def test_identifiers_are_not_reported_as_credentials(self):
        value='https://docs.google.com/spreadsheets/d/'+'A1b2C3d4'*4+'/edit'
        result=inspect(value.encode())
        self.assertEqual(result[0]['category'],'sensitive_google_identifier')
        self.assertNotIn(value,json.dumps(result))
        self.assertEqual(inspect(b'https://drive.google.com/file/d/'),[])

    def test_archive_payload_is_scanned_without_extracting_files(self):
        import io,zipfile
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('payload.txt','ghp_'+'Q7w9AbCd'*5)
        found,gaps=inspect_payload(stream.getvalue())
        self.assertTrue(any(f['category']=='archive_github_token_candidate' for f in found))
        self.assertEqual(gaps,[])

    def test_provenance_loads_from_private_environment_without_default_identifier(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(source_sheet_url())
        value='https://docs.google.com/spreadsheets/d/fixture-only/edit'
        with patch.dict(os.environ,{'ARGUS_SOURCE_SHEET_URL':value}):self.assertEqual(source_sheet_url(),value)

    def test_invalid_provenance_is_rejected_without_echoing_value(self):
        value='https://user:SECRET@example.test/private'
        with patch.dict(os.environ,{'ARGUS_SOURCE_SHEET_URL':value}):
            with self.assertRaises(ValueError) as error:source_sheet_url()
        self.assertEqual(str(error.exception),'invalid_source_sheet_configuration')


class WorkflowPolicyTests(unittest.TestCase):
    def test_public_ci_has_no_production_secret_or_privileged_pr_path(self):
        import yaml
        path=Path(__file__).resolve().parents[2]/'.github/workflows/public-validation.yml'
        text=path.read_text();workflow=yaml.load(text,Loader=yaml.BaseLoader)
        self.assertNotIn('secrets.',text)
        self.assertNotIn('pull_request_target',workflow['on'])
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        self.assertIn('pull_request',workflow['on'])
        for job in workflow['jobs'].values():
            self.assertEqual(job['runs-on'],'ubuntu-24.04')
            self.assertEqual(job['if'],'github.event.repository.private == false')
            self.assertLessEqual(int(job['timeout-minutes']),60)
            for step in job['steps']:
                if 'uses' in step:
                    self.assertRegex(step['uses'],r'@[0-9a-f]{40}$')
        self.assertNotIn('hostless deploy',text)
        self.assertNotIn('push: true',text)


class ReviewBoundaryTests(unittest.TestCase):
    def test_real_tokens_and_loopback_credentials_are_not_ignored_by_words(self):
        value='ghp_'+'example'+'A9b8C7d6'*5
        self.assertTrue(any(f['category']=='github_token_candidate' for f in inspect(value.encode())))
        dsn=('postgres'+'://user:'+'A9b8C7d6'*4+'@localhost/private').encode()
        self.assertTrue(any(f['category']=='database_credential_candidate' for f in inspect(dsn)))

    def test_review_applies_only_to_exact_content_path_rule_line_and_blob(self):
        import hashlib
        from publication_audit import annotate_reviews
        data=b'reviewed synthetic fixture';finding={'category':'database_credential_candidate','line':3}
        review={'path':'fixture.py','sha256':hashlib.sha256(data).hexdigest(),'blob':'a'*40,**finding,'disposition':'reviewed_disposable_fixture'}
        def disposition(body=data,path='fixture.py',row=finding,oid='a'*40):
            return annotate_reviews([row],path,body,[review],oid)[0].get('disposition')
        self.assertEqual(disposition(),'reviewed_disposable_fixture')
        self.assertIsNone(disposition(body=data+b'changed credential'))
        self.assertIsNone(disposition(path='another.py'))
        self.assertIsNone(disposition(row={**finding,'line':4}))
        self.assertIsNone(disposition(row={**finding,'category':'private_key_material'}))
        self.assertIsNone(disposition(oid='b'*40))

    def test_disposable_compose_uses_fresh_private_credentials(self):
        from setup_source_adapter import fixture_services,write_fixture_compose
        from urllib.parse import urlsplit
        first=fixture_services();second=fixture_services()
        password=first['db']['environment']['POSTGRES_PASSWORD']
        self.assertTrue(len(password)>=24,'fixture credential too short')
        self.assertTrue(password!=second['db']['environment']['POSTGRES_PASSWORD'],'fixture credential reused')
        parsed=urlsplit(first['api']['environment']['DATABASE_URL'])
        self.assertTrue(parsed.password==password,'fixture credential inconsistent')
        self.assertTrue(parsed.hostname=='db','fixture service not isolated')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'compose.json';write_fixture_compose(path)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):write_fixture_compose(path)


class IndependentScanGateTests(unittest.TestCase):
    def test_missing_established_scanner_is_blocked_not_a_clean_result(self):
        from independent_scan import run
        with tempfile.TemporaryDirectory() as directory,patch('independent_scan.shutil.which',return_value=None),patch('independent_scan.subprocess.run') as process:
            result=run(Path(directory))
        self.assertEqual(result['status'],'BLOCKED_BY_ENVIRONMENT')
        self.assertFalse(result['executed'])
        self.assertEqual(set(result['scopes']),{'all_tracked_files','all_git_history','docker_context_superset','workflow_files','committed_fixtures'})
        self.assertTrue(all(value=='NOT_EXECUTED' for value in result['scopes'].values()))
        process.assert_not_called()
