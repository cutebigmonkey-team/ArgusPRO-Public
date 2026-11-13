import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from pending_recovery import recover


class RecoveryTests(unittest.TestCase):
    def worker(self):
        return SimpleNamespace(STOP_REQUESTED=Mock(is_set=Mock(return_value=False),wait=Mock(return_value=False)),
                               emit=Mock(),recover_drive=Mock())
    def test_transient_drive_failure_retries_same_index_and_package(self):
        w=self.worker();w.recover_drive.side_effect=[TimeoutError('secret'),None]
        journal=Mock();feed=object();store=object()
        self.assertTrue(recover(w,store,journal,feed))
        self.assertEqual(w.recover_drive.call_count,2)
        self.assertTrue(all(c.args==(store,journal,feed) for c in w.recover_drive.call_args_list))
        w.STOP_REQUESTED.wait.assert_called_once()
        self.assertNotIn('secret',str(w.emit.call_args_list))
    def test_retry_after_and_shutdown_preserve_recovery_slot(self):
        w=self.worker();error=TimeoutError();error.headers={'Retry-After':'3600'}
        w.recover_drive.side_effect=error;w.STOP_REQUESTED.wait.return_value=True
        self.assertFalse(recover(w,Mock(),Mock(),Mock()))
        self.assertGreaterEqual(w.STOP_REQUESTED.wait.call_args.args[0],3600)
    def test_corruption_and_lost_session_are_not_retried(self):
        w=self.worker();w.recover_drive.side_effect=ValueError('checksum')
        with self.assertRaises(RuntimeError):recover(w,Mock(),Mock(),Mock())
        w.STOP_REQUESTED.wait.assert_not_called()
        w=self.worker();journal=Mock();journal.check_session.side_effect=RuntimeError('lost session')
        with self.assertRaises(RuntimeError):recover(w,Mock(),journal,Mock())
        w.recover_drive.assert_not_called()
