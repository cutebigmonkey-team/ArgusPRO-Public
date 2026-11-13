"""HTTP security boundaries: no real collection and no production database writes."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from archive_api import Server, PREFIX, byte_range
from auth_store import AuthStore

TEST_KEY = 'test-only-key-123456'


class FakeArchive:
    def __init__(self, root):
        self.root = root
        self.config = {'label': 'Test archive'}
        AuthStore(root / 'private/users.sqlite3').create('tester', TEST_KEY)
        self.movie = root / 'movie'
        self.movie.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'0' * 80)
        self.html = root / 'untrusted'
        self.html.write_bytes(b'<html><script>alert(1)</script></html>')
        self.items = []

    def materials(self, _):
        return {'items': self.items, 'total': len(self.items)}

    def media(self, sha):
        return self.movie if sha == 'a' * 64 else self.html if sha == 'b' * 64 else None


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'index.html').write_text('<h1>Public shell only</h1>')
        self.archive = FakeArchive(self.root)
        self.server = Server(0, self.archive, self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def call(self, path, method='GET', data=None, headers=None):
        request_headers = {'Origin': self.server.origin}
        if self.cookie:
            request_headers['Cookie'] = self.cookie
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self):
        code, headers, _ = self.call(PREFIX + '/login', 'POST', {'username': 'tester', 'key': TEST_KEY})
        self.assertEqual(code, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def test_all_data_and_media_require_authentication(self):
        for method in ('GET', 'HEAD'):
            for path in ('/session', '/materials', '/media/' + 'a' * 64):
                self.assertEqual(self.call(PREFIX + path, method)[0], 401)
        self.assertEqual(self.call('/')[0], 200)
        self.assertEqual(self.call(PREFIX + '/login', 'POST', {'key': 'wrong'})[0], 401)

    def test_login_logout_and_expiry_invalidate_session(self):
        self.login()
        self.assertEqual(self.call(PREFIX + '/materials')[0], 200)
        self.assertEqual(self.call(PREFIX + '/logout', 'POST')[0], 200)
        self.assertEqual(self.call(PREFIX + '/materials')[0], 401)
        self.login()
        for key in self.server.sessions:
            self.server.sessions[key]['expires'] = time.time() - 1
        self.assertEqual(self.call(PREFIX + '/materials')[0], 401)

    def test_origin_host_and_csrf_protection(self):
        self.assertEqual(self.call(PREFIX + '/login', 'POST', {'key': 'test-only-key'}, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.call('/', headers={'Host': 'evil.example'})[0], 403)
        self.login()
        code, headers, _ = self.call(PREFIX + '/materials', headers={'Origin': 'https://evil.example'})
        self.assertEqual(code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(self.call(PREFIX + '/materials', 'OPTIONS')[0], 403)

    def test_media_ranges_and_active_content_download(self):
        self.login()
        code, headers, body = self.call(PREFIX + '/media/' + 'a' * 64, headers={'Range': 'bytes=4-7'})
        self.assertEqual((code, body), (206, b'ftyp'))
        self.assertEqual(headers['Content-Type'], 'video/mp4')
        self.assertEqual(self.call(PREFIX + '/media/' + 'a' * 64, headers={'Range': 'bytes=9999-'})[0], 416)
        code, headers, _ = self.call(PREFIX + '/media/' + 'b' * 64 + '?name=page.html')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertIn('sandbox', headers['Content-Security-Policy'])

    def test_no_static_storage_access_or_write_api(self):
        self.login()
        for path in ('/private/archive-api.json', '/media/secret', '/../private/access-key.txt', '/fixtures/posts.json'):
            self.assertEqual(self.call(path)[0], 404)
        self.assertEqual(self.call(PREFIX + '/materials', 'POST', {'text': 'write'})[0], 405)

    def test_subsequent_request_observes_committed_import(self):
        self.login()
        self.assertEqual(json.loads(self.call(PREFIX + '/materials')[2])['total'], 0)
        self.archive.items = [{'id': 'committed-import'}]
        self.assertEqual(json.loads(self.call(PREFIX + '/materials')[2])['total'], 1)

    def test_login_rate_limit(self):
        for _ in range(20):
            self.assertEqual(self.call(PREFIX + '/login', 'POST', {'key': 'wrong'})[0], 401)
        self.assertEqual(self.call(PREFIX + '/login', 'POST', {'key': 'test-only-key'})[0], 429)

    def test_suffix_range_validation(self):
        self.assertEqual(byte_range('bytes=-10', 100), (90, 99, 206))
        for header in ('bytes=-0', 'bytes=7-3', 'bytes=0-1,3-4', 'bytes=-'):
            with self.assertRaises(ValueError):
                byte_range(header, 100)

    def test_individual_users_and_immediate_revocation(self):
        self.login()
        self.assertEqual(json.loads(self.call(PREFIX + '/session')[2])['username'], 'tester')
        self.server.auth.create('second', 'another-person-key-123')
        self.assertEqual(self.call(PREFIX + '/login', 'POST', {'username': 'second', 'key': TEST_KEY})[0], 401)
        self.server.auth.reset_key('tester', 'replacement-person-key')
        self.assertEqual(self.call(PREFIX + '/materials')[0], 401)
        self.assertEqual(self.call(PREFIX + '/login', 'POST', {'username': 'tester', 'key': TEST_KEY})[0], 401)
        code, headers, _ = self.call(PREFIX + '/login', 'POST', {'username': 'tester', 'key': 'replacement-person-key'})
        self.assertEqual(code, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.server.auth.set_enabled('tester', False)
        self.assertEqual(self.call(PREFIX + '/media/' + 'a' * 64)[0], 401)
        self.server.auth.set_enabled('tester', True)
        self.assertEqual(self.call(PREFIX + '/materials')[0], 401)

    def test_keys_are_never_stored_as_plaintext(self):
        self.assertNotIn(TEST_KEY.encode(), (self.root / 'private/users.sqlite3').read_bytes())
        self.assertEqual(self.call(PREFIX + '/login', 'POST', {'key': TEST_KEY})[0], 401)


if __name__ == '__main__':
    unittest.main()
