import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from contextlib import nullcontext
from comment_maintenance import CommentMaintenance


class CommentMaintenanceTests(unittest.TestCase):
    def fixture(self,n=300):
        rows={str(i).zfill(5):{'id':str(i).zfill(5)} for i in range(n)}
        index=SimpleNamespace(get=rows.get,after=lambda cursor,limit:iter([(sid,p) for sid,p in sorted(rows.items()) if sid>(cursor or '')][:limit]))
        db=SimpleNamespace(transaction=nullcontext);emit=Mock();guard=Mock();clock=Mock(return_value=100)
        return CommentMaintenance(db,SimpleNamespace(index=index),emit,guard,clock),rows,clock

    def test_bounded_sweep_and_hints_preserve_progress(self):
        maintenance,rows,clock=self.fixture();comments=Mock()
        with patch('comments_store.Comments',return_value=comments):
            maintenance.step([]);self.assertFalse(maintenance.complete)
            self.assertEqual(len(comments.sync.call_args.args[0]),128)
            maintenance.dirty(['00299']);maintenance.step([])
            self.assertTrue(any(r['id']=='00299' for r in comments.sync.call_args_list[-2].args[0]))
            for _ in range(4):maintenance.step([])
        self.assertTrue(maintenance.complete);self.assertEqual(maintenance.status,'ok')
        self.assertTrue(all(len(call.args[0])<=128 for call in comments.sync.call_args_list))

    def test_initialization_failure_does_not_block_admission_and_retries(self):
        maintenance,_,clock=self.fixture();comments=Mock()
        with patch('comments_store.Comments',side_effect=[RuntimeError('private'),comments]):
            maintenance.step([]);self.assertEqual(maintenance.status,'degraded')
            maintenance.step([]);self.assertIsNone(maintenance.comments)
            clock.return_value=103;maintenance.step([])
        self.assertIs(maintenance.comments,comments);maintenance.guard.assert_called()
        self.assertNotIn('private',str(maintenance.emit.call_args_list))

    def test_failed_chunk_does_not_advance_cursor_or_drop_hints(self):
        maintenance,_,clock=self.fixture();comments=Mock();comments.sync.side_effect=RuntimeError('private')
        maintenance.dirty(['00001'])
        with patch('comments_store.Comments',return_value=comments):maintenance.step([])
        self.assertIsNone(maintenance.cursor);self.assertIn('00001',maintenance.hints)
        self.assertEqual(maintenance.status,'degraded')

    def test_hint_capacity_and_lost_session(self):
        maintenance,_,_=self.fixture();maintenance.dirty(map(str,range(10000)))
        self.assertEqual(len(maintenance.hints),1024)
        maintenance.guard.side_effect=RuntimeError('lost session')
        with self.assertRaises(RuntimeError):maintenance.step([])

    def test_empty_delivery_does_not_make_completed_comments_partial(self):
        maintenance,_,_=self.fixture(0)
        with patch('comments_store.Comments',return_value=Mock()):maintenance.step([])
        self.assertTrue(maintenance.complete)
        maintenance.dirty([]);self.assertTrue(maintenance.complete)
        maintenance.rescan();self.assertFalse(maintenance.complete)
