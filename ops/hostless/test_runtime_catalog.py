import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from runtime_catalog import load_private_catalog

CATALOG={'sources':[{'id':'fixture-source','url':'https://example.invalid/feed','platform':'rss','enabled':True}]}

class PrivateCatalogTests(unittest.TestCase):
    def test_existing_drive_catalog_keeps_source_identity(self):
        store=Mock();store.read_json.return_value=CATALOG
        with patch.dict(os.environ,{'ARGUS_RUNTIME_MODE':'collect','ARGUS_MIN_PRIVATE_SOURCES':'1'},clear=True):
            value=load_private_catalog(store,lambda store,catalog:catalog)
        self.assertEqual(value,CATALOG);store.read_json.assert_called_once_with('sources.json')

    def test_no_shipped_fixture_fallback_for_missing_or_incomplete_private_catalog(self):
        for catalog in (None,{'sources':[]},CATALOG):
            store=Mock();store.read_json.return_value=catalog
            with patch.dict(os.environ,{'ARGUS_RUNTIME_MODE':'collect','ARGUS_MIN_PRIVATE_SOURCES':'2'},clear=True):
                with self.assertRaises(ValueError):load_private_catalog(store,lambda store,catalog:catalog)

    def test_collect_requires_owner_completeness_confirmation(self):
        store=Mock();store.read_json.return_value=CATALOG
        with patch.dict(os.environ,{'ARGUS_RUNTIME_MODE':'collect'},clear=True):
            with self.assertRaisesRegex(ValueError,'completeness'):load_private_catalog(store,lambda store,catalog:catalog)

    def test_private_file_outside_source_is_supported_without_drive_catalog_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sources.private.json';path.write_text(json.dumps(CATALOG));store=Mock()
            with patch.dict(os.environ,{'ARGUS_PRIVATE_SOURCE_FILE':str(path)},clear=True):
                self.assertEqual(load_private_catalog(store,lambda store,catalog:catalog),CATALOG)
            store.read_json.assert_not_called()

    def test_duplicate_ids_and_in_tree_path_rejected(self):
        store=Mock();store.read_json.return_value={'sources':CATALOG['sources']*2}
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):load_private_catalog(store,lambda store,catalog:catalog)
        with patch.dict(os.environ,{'ARGUS_PRIVATE_SOURCE_FILE':str(Path(__file__).resolve())},clear=True):
            with self.assertRaises(ValueError):load_private_catalog(store,lambda store,catalog:catalog)


class PrivateCatalogFailureTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'ARGUS_RUNTIME_MODE':'collect','ARGUS_MIN_PRIVATE_SOURCES':'1'},clear=True)
        self.environment.start();self.addCleanup(self.environment.stop)
        self.store=Mock()

    def load(self):return load_private_catalog(self.store,lambda store,catalog:catalog)

    def test_invalid_file_json_is_redacted_and_never_falls_back_to_drive(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sources.private.json';path.write_text('{"SECRET-private-value"')
            with patch.dict(os.environ,{'ARGUS_PRIVATE_SOURCE_FILE':str(path)}):
                with self.assertRaisesRegex(ValueError,'^private_catalog_invalid_json$') as raised:self.load()
        self.assertNotIn('SECRET',str(raised.exception));self.store.read_json.assert_not_called()

    def test_invalid_drive_json_is_redacted(self):
        self.store.read_json.side_effect=json.JSONDecodeError('SECRET','SECRET',0)
        with self.assertRaisesRegex(ValueError,'^private_catalog_invalid_json$') as raised:self.load()
        self.assertNotIn('SECRET',str(raised.exception))

    def test_unavailable_drive_blocks_collection_without_writes(self):
        self.store.read_json.side_effect=TimeoutError('SECRET upstream detail')
        with self.assertRaisesRegex(ValueError,'^private_catalog_unavailable$'):self.load()
        self.assertEqual([c[0] for c in self.store.mock_calls],['read_json'])

    def test_invalid_credentials_block_without_retry_or_fallback(self):
        from urllib.error import HTTPError
        for error in [PermissionError('SECRET'),HTTPError('https://example.invalid',401,'SECRET',{},None),HTTPError('https://example.invalid',403,'SECRET',{},None)]:
            self.store.reset_mock();self.store.read_json.side_effect=error
            with self.assertRaisesRegex(ValueError,'^private_catalog_access_denied$') as raised:self.load()
            self.assertNotIn('SECRET',str(raised.exception));self.assertEqual(self.store.read_json.call_count,1)

    def test_rate_limit_is_not_retried_early(self):
        from urllib.error import HTTPError
        self.store.read_json.side_effect=HTTPError('https://example.invalid',429,'SECRET',{'Retry-After':'600'},None)
        with self.assertRaisesRegex(ValueError,'^private_catalog_rate_limited$'):self.load()
        self.assertEqual(self.store.read_json.call_count,1)

    def test_next_attempt_recovers_without_resetting_source_identity_or_pending(self):
        self.store.read_json.side_effect=[TimeoutError('SECRET'),CATALOG]
        with self.assertRaisesRegex(ValueError,'private_catalog_unavailable'):self.load()
        recovered=self.load()
        self.assertEqual(recovered,CATALOG)
        self.assertEqual(self.store.read_json.call_count,2)
        self.assertTrue(all(c[0]=='read_json' for c in self.store.mock_calls))

    def test_submission_access_failure_is_redacted(self):
        self.store.read_json.return_value=CATALOG
        with self.assertRaisesRegex(ValueError,'^private_catalog_access_denied$'):
            load_private_catalog(self.store,Mock(side_effect=PermissionError('SECRET')))


class EmptyRegistryBoundaryTests(unittest.TestCase):
    def test_empty_registry_cannot_be_replaced_with_submission_fixture(self):
        store=Mock();store.read_json.return_value={'sources':[]}
        submissions=Mock(return_value=CATALOG)
        with patch.dict(os.environ,{'ARGUS_RUNTIME_MODE':'collect','ARGUS_MIN_PRIVATE_SOURCES':'1'},clear=True):
            with self.assertRaisesRegex(ValueError,'private_source_registry_empty'):load_private_catalog(store,submissions)
        submissions.assert_not_called()
