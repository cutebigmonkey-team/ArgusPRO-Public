from email.message import Message
from io import BytesIO
import json
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import BaseHandler
from urllib.response import addinfourl

import tiktok_clip

from candidate_feeds import tiktok_rows
from candidate_media import select, video_id
from tiktok_clip import cdn_url


class Parsers(unittest.TestCase):
    def test_media_plan_accepts_unicode_separators_inside_a_jsonl_record(self):
        import candidate_media
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            posts=root/'posts.jsonl'
            posts.write_text(json.dumps({'text':'one\u2028two\u0085three','platform':'rss'},ensure_ascii=False)+'\n',encoding='utf-8')
            registry=root/'registry.json'
            registry.write_text(json.dumps({'collectors':[],'execution':{'media_fallback':True}}))
            output=root/'output'
            with patch.object(candidate_media,'require_cloud'), patch.dict('os.environ',{'GITHUB_OUTPUT':str(output)}), patch('sys.argv',[
                    'candidate_media','plan','--input',str(root),'--registry',str(registry)]):
                asyncio.run(candidate_media.main())
            self.assertIn('enabled=false',output.read_text())

    profile = {"platform": "tiktok", "url": "https://www.tiktok.com/@dsns"}
    url = "https://www.tiktok.com/@dsns/video/123456789"
    row = {"url": url, "id": "123456789", "author": "dsns", "desc": "Опис"}

    def test_feed_checks_owner_permalink_and_requested_id(self):
        rows = [self.row, self.row, {**self.row, "author": "other"}, {**self.row, "id": "999"},
                {**self.row, "url": self.url.replace("www.tiktok.com", "evil.test")}]
        self.assertEqual([i["canonical_key"] for i in tiktok_rows(self.profile, rows, 5)], ["tiktok:123456789"])
        self.assertEqual(tiktok_rows({**self.profile, "url": self.url + "0"}, rows, 5), [])

    def test_public_stream_host_validation(self):
        self.assertTrue(cdn_url("https://v16.tiktokcdn-us.com/video.mp4"))
        for url in ("http://v.tiktok.com/v", "https://evil.test/v", "https://tiktok.com.evil.test/v",
                    "https://example@v.tiktok.com/v", "https://v.tiktok.com:8080/v"):
            self.assertFalse(cdn_url(url))

    def test_signed_cdn_request_uses_its_own_page_session(self):
        url = self.url
        asset = "https://v16.tiktok.com/movie.mp4"
        payload = b"test video bytes"
        video = {"id": "123456789", "author": {"uniqueId": "dsns"},
                 "video": {"duration": 8, "width": 128, "height": 256, "playAddr": asset}}
        page = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": video}}}}
        requests = []

        class FakeTLS(BaseHandler):
            handler_order = 400
            def https_open(self, req):
                requests.append((req.full_url, req.get_header("Cookie")))
                headers = Message()
                if req.full_url == url:
                    headers["Set-Cookie"] = "tt_chain_token=test; Domain=.tiktok.com; Secure; Path=/"
                    body = b'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(page).encode() + b"</script>"
                    code = 200
                elif req.full_url == asset and req.get_header("Cookie") == "tt_chain_token=test":
                    headers["Content-Type"] = "video/mp4"
                    headers["Content-Range"] = f"bytes 0-{len(payload)-1}/{len(payload)}"
                    body, code = payload, 206
                else:
                    raise AssertionError("Unexpected request or missing session cookie")
                result = addinfourl(BytesIO(body), headers, req.full_url, code)
                result.msg = "OK"
                return result

        session = tiktok_clip.public_session()
        session.add_handler(FakeTLS())
        with tempfile.TemporaryDirectory() as tmp, patch.object(tiktok_clip, "public_session", return_value=session):
            result = tiktok_clip.extract(url, "123456789", Path(tmp), SimpleNamespace)
            self.assertEqual(result["clip_status"], "ok")
            self.assertEqual((Path(tmp) / "video.mp4").read_bytes(), payload)
        self.assertEqual(requests, [(url, None), (asset, "tt_chain_token=test")])

    def test_media_requires_exact_owned_identity_and_registry(self):
        post = {"post_url": self.url, "platform": "tiktok", "canonical_key": "tiktok:123456789"}
        registry = {"execution": {"media_fallback": True}, "collectors": [{"platform": "tiktok", "enabled": True}]}
        self.assertEqual(select([post, post, {**post, "canonical_key":"tiktok:999"}], registry), [post])
        self.assertIsNone(video_id({**post, "post_url": self.url.replace("www.tiktok.com", "evil.test")}))
        registry["execution"]["media_fallback"] = False
        self.assertEqual(select([post], registry), [])


if __name__ == "__main__":
    unittest.main()
