"""Checks the actual WSGI app's local security and session boundaries."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlsplit

from test_archive_api import FakeArchive, TEST_KEY
from web_api import App, PREFIX

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = FakeArchive(self.root)
        self.app = App(self.root, self.archive.config, archive=self.archive)
        self.cookie = ''

    def call(self, path, method='GET', data=None, headers=None):
        parsed = urlsplit(path)
        raw = json.dumps(data).encode() if data is not None else b''
        env = {'REQUEST_METHOD':method,'HTTP_HOST':'127.0.0.1:8765',
               'HTTP_ORIGIN':'http://127.0.0.1:8765','PATH_INFO':parsed.path,
               'QUERY_STRING':parsed.query,'CONTENT_LENGTH':str(len(raw)),
               'CONTENT_TYPE':'application/json','wsgi.input':io.BytesIO(raw),
               'HTTP_COOKIE':self.cookie}
        env.update(headers or {})
        result = []
        def start(status, values):
            result.extend((int(status.split()[0]), dict(values)))
        stream = self.app(env, start)
        try:
            body = b''.join(stream)
        finally:
            if hasattr(stream, 'close'): stream.close()
        return *result, body

    def login(self):
        code, headers, raw = self.call(PREFIX+'/login','POST',{'username':'tester','key':TEST_KEY})
        self.assertEqual(code,200)
        self.assertNotIn('token',json.loads(raw))
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def test_cookie_login_and_protected_media(self):
        for path in ('/materials','/session','/media/'+'a'*64):
            self.assertEqual(self.call(PREFIX+path)[0],401)
        self.login()
        self.assertEqual(json.loads(self.call(PREFIX+'/session')[2])['username'],'tester')
        code, headers, body = self.call(PREFIX+'/media/'+'a'*64,headers={'HTTP_RANGE':'bytes=4-7'})
        self.assertEqual((code,body),(206,b'ftyp'))
        self.assertEqual(headers['Content-Range'],'bytes 4-7/92')
        self.cookie = ''
        self.assertEqual(self.call(PREFIX+'/media/'+'a'*64+'?access=anything')[0],401)

    def test_local_only_even_with_proxy_headers(self):
        self.login()
        for headers in ({'HTTP_HOST':'api.example.test'}, {'HTTP_ORIGIN':'https://argusnews.online'},
                        {'HTTP_ORIGIN':'https://evil.test','HTTP_X_FORWARDED_HOST':'127.0.0.1:8765'}):
            code, response, _ = self.call(PREFIX+'/materials',headers=headers)
            self.assertEqual(code,403)
            self.assertNotIn('Access-Control-Allow-Origin',response)
        self.assertEqual(self.call(PREFIX+'/login','OPTIONS')[0],403)
        self.assertEqual(self.call(PREFIX+'/login','POST',{}, {'HTTP_ORIGIN':''})[0],403)
        with self.assertRaises(ValueError):
            App(self.root,{**self.archive.config,'public_origin':'https://api.example.test'},archive=self.archive)

    def test_logout_expiry_key_reset_disable(self):
        self.login()
        self.assertEqual(self.call(PREFIX+'/logout','POST')[0],200)
        self.assertEqual(self.call(PREFIX+'/materials')[0],401)
        self.login()
        self.app.auth.reset_key('tester','replacement-key-long-enough')
        self.assertEqual(self.call(PREFIX+'/materials')[0],401)
        self.app.auth.reset_key('tester',TEST_KEY)
        self.login()
        self.app.auth.set_enabled('tester',False)
        self.assertEqual(self.call(PREFIX+'/materials')[0],401)
        self.app.auth.set_enabled('tester',True)
        self.assertEqual(self.call(PREFIX+'/materials')[0],401)
        self.login()
        for record in self.app.sessions.values(): record['expires']=time.time()-1
        self.assertEqual(self.call(PREFIX+'/materials')[0],401)

    def test_static_allowlist_and_no_plaintext_keys(self):
        for path in ('/private/users.sqlite3','/fixtures/posts.json','/media/secret','/../private/archive-api.json'):
            self.assertEqual(self.call(path)[0],404)
        self.assertEqual(self.call(PREFIX+'/login','POST',{'key':TEST_KEY})[0],401)
        self.assertNotIn(TEST_KEY.encode(),(self.root/'private/users.sqlite3').read_bytes())
        with self.assertRaises(ValueError): self.app.auth.create('short','12345')
        self.login()
        self.assertEqual(self.call(PREFIX+'/materials','POST',{})[0],405)
        code, headers, _ = self.call(PREFIX+'/media/'+'b'*64+'?name=page.html')
        self.assertEqual(code,200)
        self.assertIn('attachment',headers['Content-Disposition'])
        self.assertIn('sandbox',headers['Content-Security-Policy'])

if __name__ == '__main__': unittest.main()
