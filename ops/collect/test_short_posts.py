import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from remaining_social import normalize
from run import collect_source
from short_posts import tiktok_row


class CatalogToolsTests(unittest.TestCase):
    def test_tiktok_rows_require_the_requested_author_and_id(self):
        source = {'platform': 'tiktok', 'url': 'https://www.tiktok.com/@example'}
        video = {'id': '123', 'author': {'uniqueId': 'Example'}, 'desc': 'Post'}
        row = tiktok_row(video, 'example', '123')
        posts = normalize(source, [row, row], 10, 'TikTok-Api')
        self.assertEqual([p['canonical_key'] for p in posts], ['tiktok:123'])
        self.assertIsNone(tiktok_row(video, 'other', None))
        self.assertIsNone(tiktok_row(video, 'example', '456'))

    def test_fbgql_is_first_and_a_block_stops_the_browser(self):
        source = {'platform': 'facebook', 'url': 'https://www.facebook.com/example'}
        attempt = {'tool': 'fbgql', 'posts': 0, 'status': 'http_403'}
        with patch.dict(os.environ, {'ARGUS_REMAINING_COLLECTION':'1'}), \
                patch('remaining_social.adapter', AsyncMock(return_value=([], attempt))), \
                patch('run.social', new_callable=AsyncMock) as browser:
            posts, status, attempts = asyncio.run(collect_source(object(), source, 5))
        self.assertEqual((posts, status, attempts), ([], 'http_403', [attempt]))
        browser.assert_not_awaited()
