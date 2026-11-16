import os
import unittest
import urllib.error
from unittest.mock import patch

from tinyfish_fetch import ENDPOINT, TinyFetch, checked_pages, owned_key


TG = {'url': 'https://t.me/fixture110', 'platform': 'telegram'}
THREADS = {'url': 'https://www.threads.com/@fixture1096', 'platform': 'threads'}
LINK = THREADS['url'] + '/post/DemiWQXDCYZ'
CAPTION = 'Публічний допис рятувальників'


def page(url, text='page', **details):
    return {'url': url, 'final_url': url, 'text': text, **details}


class TinyFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict(os.environ, TINYFISH_API_KEY='fixture'):
            self.client = TinyFetch()
            self.assertNotIn('TINYFISH_API_KEY', os.environ)

    async def test_fallback_keeps_primary_and_archival_identity(self):
        url = 'https://t.me/s/fixture110'
        markup = '<div class="tgme_widget_message" data-post="fixture110/4064"><div class="tgme_widget_message_text">Post text</div></div>'
        with patch('tinyfish_fetch.fetch_pages', return_value={'results':[page(url, markup)]}) as fetch:
            primary = [{'tool':'telegram_public_history', 'status':'timeout', 'posts':0}]
            rows, status, attempts = await self.client.recover(TG, 1, [], 'timeout', primary)
            fetch.assert_called_once_with('fixture', [url], include_selectors=['.tgme_widget_message[data-post]'])
            self.assertEqual((status, rows[0]['canonical_key']), ('ok','telegram:fixture110/4064'))
            self.assertEqual(attempts, primary + [{'tool':'tinyfish_fetch', 'status':'ok','posts':1}])

    async def test_profile_followup_maps_by_url_and_rejects_recommendations(self):
        profile = page(THREADS['url'], links=[LINK, LINK.replace('@fixture1096','@someone'), LINK, LINK+'/media'])
        detail = page(LINK, CAPTION, description=CAPTION, published_date='2026-11-02T08:00:00Z')
        with patch('tinyfish_fetch.fetch_pages', side_effect=[{'results':[profile]}, {'results':[detail]}]) as fetch:
            rows, _, _ = await self.client.recover(THREADS, 5, [], 'no_public_posts', [])
            self.assertEqual(fetch.call_args.args[1], [LINK])
            self.assertEqual([(r['canonical_key'],r['text']) for r in rows], [('threads:DemiWQXDCYZ',CAPTION)])

    async def test_skip_success_policy_stops_and_absent_key(self):
        with patch('tinyfish_fetch.fetch_pages') as fetch:
            for rows, status, attempts in [([{'canonical_key':'existing'}], 'ok', []),
                                           ([], 'disabled_by_registry', []), ([], 'http_429', []),
                                           ([], 'no_public_posts', [{'status':'robots_denied'}])]:
                self.assertEqual(await self.client.recover(TG, 1, rows, status, attempts), (rows,status,attempts))
            self.client.key = ''
            self.assertEqual(await self.client.recover(TG, 1, [], 'timeout', []), ([], 'timeout', []))
            fetch.assert_not_called()

    async def test_provider_quota_stops_runner_and_preserves_primary_status(self):
        with patch('tinyfish_fetch.fetch_pages', side_effect=urllib.error.HTTPError(ENDPOINT,429,'',{},None)) as fetch:
            rows, status, attempts = await self.client.recover(TG, 1, [], 'timeout', [])
            self.assertEqual((rows, status, attempts[-1]['status']), ([], 'timeout', 'http_429'))
            await self.client.recover(TG, 1, [], 'timeout', [])
            self.assertEqual(fetch.call_count, 1)

    def test_reject_redirected_result_or_foreign_permalink(self):
        with self.assertRaisesRegex(ValueError, 'no_verified_page'):
            checked_pages({'results':[page(LINK, final_url=THREADS['url'])]}, [LINK])
        self.assertIsNone(owned_key(THREADS, 'https://unrelated.example/@dsns_kyiv/post/1'))
        self.assertIsNone(owned_key(THREADS, LINK.replace('@fixture1096','@someone')))


if __name__ == '__main__':
    unittest.main()
