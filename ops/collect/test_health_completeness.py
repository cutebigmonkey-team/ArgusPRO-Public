import unittest
from source_health import platform_status,update


class CompletenessTests(unittest.TestCase):
    def test_deferred_siblings_prevent_success_claim(self):
        for status in ('deferred_budget','deferred_rate_limit','not_due'):
            with self.subTest(status=status):
                self.assertEqual(platform_status(2,['ok',status],3),'partial')

    def test_disabled_sibling_does_not_invalidate_verified_collection(self):
        self.assertEqual(platform_status(2,['ok','disabled_by_registry'],3),'collected')

    def test_incomplete_empty_fetch_does_not_claim_inactivity(self):
        self.assertEqual(platform_status(1,['partial_results'],0),'partial')

    def test_empty_page_with_continuation_is_not_a_successful_quiet_poll(self):
        row={'url':'https://example.org/feed','platform':'rss','status':'no_new_posts',
             'checked_at':'2026-11-13T12:00:00Z','new_keys':[],'canonical_keys':[],
             'attempts':[],'seconds':1,'remaining_ranges':2,'retry_after':'2026-11-13T13:00:00Z'}
        health=update({},row)
        self.assertEqual(health['collector_status'],'partial_results')
        self.assertIsNone(health['last_success_at'])
        self.assertEqual(health['quiet_checks'],0)
        self.assertEqual(health['remaining_ranges'],2)
        self.assertEqual(health['next_check_at'],'2026-11-13T13:00:00+00:00')
