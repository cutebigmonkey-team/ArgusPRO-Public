import os
import unittest
from unittest.mock import patch
from source_kind import kind
from run import source_valid


class SourceScopeTests(unittest.TestCase):
    def test_video_is_not_a_continuous_creator_source(self):
        self.assertEqual(kind({'platform':'tiktok','url':'https://www.tiktok.com/@user/video/123'}),'single_publication')
        self.assertEqual(kind({'platform':'tiktok','url':'https://www.tiktok.com/@user'}),'publisher')
        self.assertEqual(kind({'platform':'rutube','url':'https://rutube.ru/live/video/abc'}),'single_publication')
        self.assertEqual(kind({'platform':'tiktok','url':'https://vt.tiktok.com/example'}),'unresolved_short_link')
    def test_anonymous_runtime_rejects_session_bearing_source_url(self):
        with patch.dict(os.environ,{'ARGUS_ANONYMOUS_ONLY':'1'}):
            self.assertFalse(source_valid({'platform':'x','url':'https://x.com/example?session=private'}))
            self.assertTrue(source_valid({'platform':'x','url':'https://x.com/example'}))
