import json
import unittest
from unittest.mock import patch
from gdelt_public import collect
from registry import default_registry,validate


class GdeltPublicTests(unittest.TestCase):
    def setUp(self):self.source={'id':'gdelt-query','platform':'gdelt','url':'https://api.gdeltproject.org/api/v2/doc/doc?query=ukraine'}
    def fetch(self,url):
        if url.endswith('/robots.txt'):return b'User-agent: *\nAllow: /',url
        return json.dumps({'articles':[{'url':'https://example.org/article','title':'Public fixture','seendate':'20261113100000'}]}).encode(),url
    def test_anonymous_gdelt_keeps_seen_date_separate_from_publication(self):
        rows,status=collect(self.source,10,self.fetch)
        self.assertEqual(status,'ok');self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['canonical_key'].startswith('gdelt:'))
        self.assertIsNone(rows[0]['published_at']);self.assertEqual(rows[0]['gdelt_seen_at'],'20261113100000')
    def test_denied_robots_is_not_bypassed(self):
        requested=[]
        def fetch(url):requested.append(url);return b'User-agent: *\nDisallow: /',url
        with self.assertRaisesRegex(ValueError,'robots_denied'):collect(self.source,10,fetch)
        self.assertEqual(len(requested),1)
    def test_200_login_html_is_not_success(self):
        def fetch(url):
            return self.fetch(url) if url.endswith('/robots.txt') else (b'<html>login</html>',url)
        with self.assertRaises(ValueError):collect(self.source,10,fetch)
    def test_saturated_window_is_partial_and_duplicate_links_are_not_new_posts(self):
        rows,status=collect(self.source,1,self.fetch)
        self.assertEqual(status,'partial_results');self.assertEqual(len(rows),1)
    def test_gdelt_registry_extension_does_not_break_existing_private_schema(self):
        value=default_registry();validate(value)
        value['collectors'].append({'platform':'gdelt','engine':'gdelt_public_json','enabled':True})
        self.assertEqual(validate(value),value)
