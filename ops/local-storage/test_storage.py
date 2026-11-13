import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.request
import zipfile

import storage
from sample_package import create


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'package.zip'
        create(self.archive)

    def test_hashes_and_shared_media(self):
        with storage.validate_package(self.archive, self.root) as (manifest, posts, staging):
            self.assertTrue(manifest['sample'])
            self.assertEqual(len(posts), 2)
            self.assertEqual(posts[0]['media'], posts[1]['media'])
            self.assertEqual(len(list((staging / 'media').iterdir())), 1)

    def test_corruption_rejected(self):
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(bad, 'w') as target:
            for item in source.infolist():
                blob = source.read(item.filename)
                if item.filename == 'posts.jsonl':
                    blob = b'X' + blob[1:]
                target.writestr(item.filename, blob)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            with storage.validate_package(bad, self.root):
                pass

    def test_unlisted_traversal_rejected(self):
        with zipfile.ZipFile(self.archive, 'a') as bundle:
            bundle.writestr('../outside.txt', 'bad')
        with self.assertRaisesRegex(ValueError, 'differs'):
            with storage.validate_package(self.archive, self.root):
                pass
        self.assertFalse((self.root.parent / 'outside.txt').exists())

    def test_sample_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, 'allow-sample'):
            storage.import_package(self.archive, self.root)
        self.assertFalse((self.root / 'media').exists())

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            storage.read_json('{"a":1,"a":2}')

    def test_redirect_never_sends_github_token_to_storage(self):
        request = urllib.request.Request('https://api.github.com/test', headers={'Authorization': 'Bearer example'})
        redirect = storage.ArtifactRedirect().redirect_request(
            request, None, 302, 'Found', {}, 'https://example.blob.core.windows.net/artifact')
        self.assertIsNone(redirect.get_header('Authorization'))
        with self.assertRaises(ValueError):
            storage.ArtifactRedirect().redirect_request(request, None, 302, 'Found', {}, 'http://localhost/')


@unittest.skipUnless(os.environ.get('ARGUS_TEST_DATABASE') == '1', 'Dedicated PostgreSQL test DB required')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get('PGDATABASE') != 'argus_storage_test':
            raise RuntimeError('Tests may only reset the dedicated argus_storage_test database')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'package.zip'
        create(self.archive)
        storage.psql(self.root, 'DROP SCHEMA IF EXISTS argus_archive CASCADE;\n' +
                     Path(storage.__file__).with_name('schema.sql').read_text())

    def counts(self):
        return storage.psql(self.root, 'SELECT (SELECT count(*) FROM argus_archive.packages),'
                            '(SELECT count(*) FROM argus_archive.posts),(SELECT count(*) FROM argus_archive.media);')

    def test_replay_and_repack_do_not_duplicate_posts_or_media(self):
        first = storage.import_package(self.archive, self.root, True)
        second = storage.import_package(self.archive, self.root, True)
        self.assertEqual(first['status'], 'imported')
        self.assertEqual(second['status'], 'already_imported')
        self.assertEqual(self.counts(), '1|2|1')
        other = self.root / 'other.zip'
        create(other)  # Different manifest creation time, same posts and same media.
        storage.import_package(other, self.root, True)
        self.assertEqual(self.counts(), '2|2|1')
        self.assertEqual(len(list((self.root / 'media').glob('*/*'))), 1)
        text = storage.psql(self.root, "SELECT text_content FROM argus_archive.posts WHERE canonical_key='sample:argus:1'")
        self.assertIn('Тест імпорту', text)
        self.assertIn('newlines\n', text + '\n')

    def test_failed_transaction_is_retryable_without_duplicate_files(self):
        storage.psql(self.root, """CREATE FUNCTION argus_archive.fail_import() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN RAISE EXCEPTION 'test failure'; END $$;
            CREATE TRIGGER fail_import BEFORE INSERT ON argus_archive.posts
            FOR EACH ROW EXECUTE FUNCTION argus_archive.fail_import();""")
        with self.assertRaises(RuntimeError):
            storage.import_package(self.archive, self.root, True)
        self.assertEqual(self.counts(), '0|0|0')
        storage.psql(self.root, 'DROP TRIGGER fail_import ON argus_archive.posts;')
        storage.import_package(self.archive, self.root, True)
        self.assertEqual(self.counts(), '1|2|1')
        self.assertEqual(len(list((self.root / 'media').glob('*/*'))), 1)

    def test_older_package_does_not_overwrite_newer_post(self):
        with storage.validate_package(self.archive, self.root) as (_, posts, staging):
            posts[0]['observed_at'] = '2026-02-01T00:00:00Z'
            posts[0]['text'] = 'Updated post'
            source = self.root / 'new.jsonl'
            source.write_text(''.join(json.dumps(p) + '\n' for p in posts), encoding='utf-8')
            newer = self.root / 'new.zip'
            storage.pack(source, staging / 'media', newer, sample=True)
        storage.import_package(newer, self.root, True)
        storage.import_package(self.archive, self.root, True)
        self.assertEqual(storage.psql(self.root, "SELECT text_content FROM argus_archive.posts WHERE canonical_key='sample:argus:1'"), 'Updated post')


if __name__ == '__main__':
    unittest.main()
