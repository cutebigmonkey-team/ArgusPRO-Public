"""Offline safety contract for the isolated staging bootstrap."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from staging_http import load_source_policy, validate_staging_config, connect_staging, serve

BASE = {
    'ARGUS_STAGING_GATEWAY_ENABLED': '1',
    'ARGUS_STAGING_PG_DSN': 'postgresql://argus_ci@127.0.0.1:5432/argus_ingest_ci',
    'ARGUS_STAGING_SOURCE_POLICY': '/tmp/argus-staging-private-sources.json',
    'ARGUS_STAGING_REPOSITORY_ID': '12345',
    'ARGUS_STAGING_OWNER_ID': '67890',
}


class StagingGatewayTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with self.assertRaisesRegex(ValueError, 'staging_gateway_disabled'):
            validate_staging_config({})

    def test_requires_isolated_loopback_database_and_role(self):
        for dsn in (
            'postgresql://postgres@127.0.0.1/argus_ingest_ci',
            'postgresql://argus_ci@example.org/argus_ingest_ci',
            'postgresql://argus_ci@127.0.0.1/argus_production',
            'postgresql://argus_ci@127.0.0.1/argus_ingest_ci?hostaddr=1.2.3.4',
        ):
            with self.subTest(dsn=dsn), self.assertRaises(ValueError):
                validate_staging_config({**BASE, 'ARGUS_STAGING_PG_DSN': dsn})

    def test_archival_configuration_fails_closed(self):
        for key in ('ARGUS_ARCHIVE_WRITER_OWNER', 'ARGUS_DRIVE_OAUTH', 'ARGUS_JSONBIN_KEY'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_staging_config({**BASE, key: 'set'})

    def test_source_policy_rejects_unknown_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'sources.json'
            p.write_text(json.dumps({'sources': [{'id': 'rss_test', 'platform': 'rss'}]}))
            policy = load_source_policy(p)
            self.assertTrue(policy({'source': {'source_id': 'rss_test', 'platform': 'rss'}}, {}))
            self.assertFalse(policy({'source': {'source_id': 'rss_test', 'platform': 'facebook'}}, {}))
            self.assertFalse(policy({'source': {'source_id': 'unknown', 'platform': 'rss'}}, {}))
            self.assertFalse(policy({'recover_source_id': 'rss_test'}, {}))

    def test_duplicate_ids_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'sources.json'
            p.write_text(json.dumps({'sources': [
                {'id': 'rss_test', 'platform': 'rss'},
                {'id': 'rss_test', 'platform': 'rss'}]}))
            with self.assertRaisesRegex(ValueError, 'invalid_private_source_policy'):
                load_source_policy(p)

    def test_no_listener_or_db_connection_when_disabled(self):
        with self.assertRaisesRegex(ValueError, 'staging_gateway_disabled'):
            serve({}, server_factory=lambda *a, **kw: self.fail('listener called'))

    def test_listener_is_loopback_and_connection_is_closed(self):
        from unittest.mock import Mock
        conn = Mock()
        gateway = Mock()
        gateway.http = Mock()

        class Server:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def serve_forever(self): pass

        def factory(host, port, app, *, handler_class):
            self.assertEqual((host, port), ('127.0.0.1', 8768))
            self.assertIs(app, gateway.http)
            self.assertEqual(handler_class.__name__, 'QuietHandler')
            return Server()

        with patch('staging_http.connect_staging', return_value=conn), \
             patch('staging_http.build_gateway', return_value=gateway):
            serve(BASE, server_factory=factory)
        conn.close.assert_called_once()

    def test_schema_does_not_autocreate(self):
        self.assertEqual(validate_staging_config(BASE)['dbname'], 'argus_ingest_ci')


if __name__ == '__main__':
    unittest.main()
