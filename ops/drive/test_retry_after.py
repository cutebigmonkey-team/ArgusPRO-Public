import unittest
from types import SimpleNamespace
from retry_after import retry_after


class RetryAfterTests(unittest.TestCase):
    def test_seconds_are_not_capped_at_local_backoff(self):
        self.assertEqual(retry_after(SimpleNamespace(resp={'Retry-After':'3600'})),3600)

    def test_http_date_and_case_insensitive_header(self):
        self.assertEqual(retry_after(SimpleNamespace(headers={'RETRY-AFTER':'Thu, 01 Jan 1970 01:00:00 GMT'}),now=60),3540)

    def test_invalid_or_past_is_not_an_infinite_delay(self):
        for value in ('nan','inf','invalid','-1'):
            self.assertEqual(retry_after(SimpleNamespace(resp={'retry-after':value}),now=60),0)
