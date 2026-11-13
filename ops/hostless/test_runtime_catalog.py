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
