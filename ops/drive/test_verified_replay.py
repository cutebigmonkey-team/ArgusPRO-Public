import unittest
from verified_replay import VerifiedReplay


class ReplayTests(unittest.TestCase):
    def test_reverse_replay_is_bounded_and_verified(self):
        with VerifiedReplay(max_pages=2,max_bytes=100) as spool:
            spool.append({'n':1});spool.append({'n':2})
            self.assertEqual(list(spool),[{'n':2},{'n':1}])
            with self.assertRaisesRegex(ValueError,'capacity'):spool.append({'n':3})
        self.assertTrue(spool.file.closed)

    def test_disk_corruption_and_byte_limit_fail_closed(self):
        with VerifiedReplay(max_bytes=10) as spool:
            with self.assertRaisesRegex(ValueError,'capacity'):spool.append({'text':'too long'})
            spool.append({'n':1});spool.file.seek(0);spool.file.write(b'!')
            with self.assertRaisesRegex(ValueError,'corruption'):list(spool)
