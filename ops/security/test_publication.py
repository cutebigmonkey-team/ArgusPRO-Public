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
