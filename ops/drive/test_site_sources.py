import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from collection_state import commit  # installs the collector module search path
from catalog_scope import due_batch
from site_sources import with_submissions, source_row


def submission(file_id, url='https://t.me/s/test_channel'):
    data = {'name':'Test channel', 'url':url, 'region':'', 'origin':'general'}
    return {'id':file_id, 'description':json.dumps(data), 'createdTime':'2026-01-01'}


class SiteSourceTests(unittest.TestCase):
    def test_concurrent_submissions_across_pages_schedule_once(self):
        store = Mock()
        api = store.service.files.return_value.list.return_value
        api.execute.side_effect = [
            {'files':[submission('abcdefghij1')], 'nextPageToken':'next'},
            {'files':[submission('abcdefghij2','https://t.me/Test_Channel')]},
        ]
        catalog = with_submissions(store, {'sources':[]})
        plan = due_batch(catalog, {}, {'telegram'}, datetime.now(timezone.utc))
        self.assertEqual([s['id'] for s in plan['sources']], ['web-abcdefghij1'])
        self.assertEqual(plan['sources'][0]['url'], 'https://t.me/test_channel')
        catalog['sources'][0]['enabled'] = False
        api.execute.side_effect = [{'files':[submission('abcdefghij3')]}]
        self.assertEqual(with_submissions(store,catalog),catalog)

    def test_reject_uncollectable_submissions(self):
        for url in ('http://127.0.0.1', 'https://node.local', 'https://t.me/+secret',
                    'https://web.telegram.org/a/#-100123', 'https://user:pass@example.org'):
            self.assertIsNone(source_row(submission('abcdefghij1', url)))
