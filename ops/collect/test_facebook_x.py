import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from cloud_social import tool_root
from facebook_x import enrich_one, facebook_command, facebook_rows
from run import collect_source
from x_post_details import x_post


class PublicReaders(unittest.TestCase):
    fb = {'platform': 'facebook', 'url': 'https://www.facebook.com/dsns'}
    twitter = {'platform': 'x', 'url': 'https://x.com/alice/status/123'}

    def test_facebook_requires_anonymous_owned_permalink(self):
        link = self.fb['url'] + '/posts/12345/'
        row = {'tier': 0, 'id': '12345', 'url': link, 'message': {'text': 'Public post'}}
        self.assertEqual(facebook_command(self.fb, 3), ['feed', 'dsns', '-n', '3'])
        items = facebook_rows(self.fb, [row, row, {**row, 'tier': 1},
            {**row, 'url': link + '?comment_id=3'}, {**row, 'url': link.replace('dsns', 'other')}], 3)
        self.assertEqual([(i['canonical_key'], i['text']) for i in items], [('facebook:12345', 'Public post')])

    @unittest.skipUnless((tool_root() / 'facebook-cli').is_dir(), 'pinned upstream fixtures not installed')
    def test_numeric_post_returned_with_opaque_permalink(self):
        row = json.loads((tool_root() / 'facebook-cli/fb/testdata/golden/post_post_perm.json').read_text(encoding='utf-8'))
        url = 'https://www.facebook.com/NASA/posts/' + row['id']
        item, = facebook_rows({**self.fb, 'url': url}, [row], 1)
        self.assertEqual((item['facebook_story_id'], item['text']), (row['id'], row['message']['text']))

    @unittest.skipUnless((tool_root() / 'x2md').is_dir(), 'pinned renderer not installed')
    def test_article_requires_matching_author_and_status(self):
        tweet = {'id': '123', 'url': self.twitter['url'], 'author': {'screen_name': 'Alice'},
                 'article': {'title': 'Title', 'content': {'blocks': [{'type': 'unstyled', 'text': 'Body'}]}}}
        with patch('x_post_details.PublicHTTP.request', side_effect=lambda url: json.dumps({'code': 200, 'tweet': tweet})):
            self.assertIn('Body', x_post(tool_root(), self.twitter)[0]['text'])
            tweet['author']['screen_name'] = 'different'
            self.assertEqual(x_post(tool_root(), self.twitter), [])

    def test_collection_entry_preserves_preceding_access_wall(self):
        attempts = [{'tool': 'facebook_public_feed', 'status': 'http_429', 'posts': 0}]
        with patch.dict(os.environ, {'ARGUS_FACEBOOK_X': '1', 'ARGUS_REVIEWED_FACEBOOK_CLI':'1', 'ARGUS_REMAINING_COLLECTION': '0'}), \
                patch('facebook_feed.collect', new_callable=AsyncMock, return_value=([], 'http_429', attempts)), \
                patch('facebook_x.read', new_callable=AsyncMock) as extra:
            _, status, _ = asyncio.run(collect_source(object(), self.fb, 5))
            self.assertEqual(status, 'http_429')
            extra.assert_not_awaited()

    def test_reviewed_reader_supplements_short_page_without_duplicate(self):
        rows = facebook_rows(self.fb, [
            {'tier': 0, 'id': key, 'url': self.fb['url']+'/posts/'+key,
             'message': {'text': 'Post '+key}} for key in ('12345','12346')], 3)
        with patch.dict(os.environ, {'ARGUS_REVIEWED_FACEBOOK_CLI':'1', 'ARGUS_REMAINING_COLLECTION':'0'}), \
                patch('facebook_feed.collect', new_callable=AsyncMock, return_value=(
                    rows[:1], 'ok', [{'tool':'facebook_public_feed','status':'ok','posts':1}])), \
                patch('facebook_x.read', new_callable=AsyncMock, return_value=(
                    rows, {'tool':'facebook-cli','status':'ok','posts':2})):
            items,status,attempts=asyncio.run(collect_source(object(), self.fb, 3))
        self.assertEqual([r['canonical_key'] for r in items], ['facebook:12345','facebook:12346'])
        self.assertEqual(status,'ok')
        self.assertEqual([a['posts'] for a in attempts],[1,2])

    def test_enrichment_preserves_existing_post_on_failure_and_other_feed_items(self):
        item = {'post_url': self.twitter['url'], 'canonical_key': 'x:123', 'text': 'Preview',
                'media_urls': [], 'published_at': None}
        next_item = {**item, 'canonical_key': 'x:999', 'post_url': self.twitter['url'].replace('123', '999')}
        for status in ('http_429', 'ok'):
            attempts = []
            seen = [item.copy(), next_item.copy()]
            rows = [{**item, 'text': 'A complete article body'}] if status == 'ok' else []
            with patch('facebook_x.read', new_callable=AsyncMock, return_value=(rows, {'status': status, 'tool': 'x2md'})) as read:
                asyncio.run(enrich_one(self.twitter, seen, attempts))
                self.assertEqual(read.await_count, 1)
            self.assertEqual(seen[0]['text'], rows[0]['text'] if rows else item['text'])
            self.assertEqual(seen[1], next_item)
            self.assertEqual(attempts[0]['status'], status)
