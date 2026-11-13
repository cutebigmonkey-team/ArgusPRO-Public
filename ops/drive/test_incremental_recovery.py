import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from publish_incremental import PENDING, drain
from publish import load_snapshot
from storage import pack
from test_publish import MemoryStore


class IncrementalRecoveryTests(unittest.TestCase):
    def test_crash_after_archive_then_recovery_with_next_batch(self):
        self.check_recovery('commit')

    def test_crash_before_readback_keeps_cursor_then_recovers_both_batches(self):
        self.check_recovery('readback')

    def check_recovery(self, phase):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'media').mkdir()
            def batch(seq):
                key = f'telegram:channel/{seq}'
                when = f'2026-11-02T10:0{seq}:00+00:00'
                post = {'canonical_key':key,'platform':'telegram','post_url':f'https://t.me/channel/{seq}',
                        'text':'Post','observed_at':when,'published_at':None,'source_id':'channel',
                        'source_url':'https://t.me/channel','media':[]}
                source=root/'posts'
                source.write_text(json.dumps(post))
                path=root/f'{seq}.zip'
                pack(source,root/'media',path)
                row = {'source_id':'channel','url':'https://t.me/channel','platform':'telegram','posts':1,
                       'canonical_keys':[key],'checked_at':when,'status':'ok','attempts':[],'seconds':1}
                return path, {'run_id':str(seq),'incremental':True,'updated_at':when,'sources':[row]}
            first,second = batch(1),batch(2)
            def interrupted_readback():
                self.assertEqual(len(load_snapshot(store)['items']),1)
                self.assertIsNone(store.read_json('collection-cursors.json'))
                raise RuntimeError('crash before independent readback')
            if phase=='readback':
                with self.assertRaises(RuntimeError):
                    drain(store,*first,before_readback=interrupted_readback)
            else:
                with patch('publish_incremental.commit',side_effect=RuntimeError('crash')):
                    with self.assertRaises(RuntimeError):drain(store,*first)
            self.assertEqual(store.read_json(PENDING)['state'],'pending')
            self.assertIsNone(store.read_json('collection-cursors.json'))
            readbacks=[]
            drain(store,*second,before_readback=lambda:readbacks.append(len(load_snapshot(store)['items'])))
            self.assertEqual(readbacks,[1,2])
            self.assertEqual(store.read_json(PENDING)['run_id'],'2')
            self.assertEqual(len(load_snapshot(store)['items']),2)
            self.assertEqual(second[1]['new_to_archive'],1)
            self.assertEqual(store.read_json('collection-cursors.json')['incremental_polling']['channel']['collector_status'],'ok')
