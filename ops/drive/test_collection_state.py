import unittest
from unittest.mock import patch
from test_publish import MemoryStore
from collection_state import commit


class CheckpointTests(unittest.TestCase):
    def report(self):
        return {'run_id':'1','sources':[{'source_id':'tg','url':'https://t.me/example','platform':'telegram','canonical_keys':['telegram:example/1']}],
            'state_updates':{'tg':{'url':'https://t.me/example','before':10,'exhausted':False,'updated_at':'2026-10-30T12:00:00Z'}}}

    def test_cursor_cannot_move_before_post_is_durable(self):
        store=MemoryStore()
        with patch('collection_state.load_snapshot',return_value={'items':[]}):
            with self.assertRaisesRegex(ValueError,'cursor_before_archive'):commit(store,self.report())
        self.assertEqual(store.files,{})

    def test_committed_cursor_replays_without_duplicates(self):
        store=MemoryStore()
        with patch('collection_state.load_snapshot',return_value={'items':[{'id':'telegram:example/1','platform':'telegram'}]}):
            result=commit(store,self.report());before=dict(store.files);commit(store,self.report())
        self.assertEqual(result['telegram_sources_with_cursor'],1);self.assertEqual(before,store.files)

    def test_returned_state_still_requires_physical_readback(self):
        store=MemoryStore();snapshot={'items':[{'id':'telegram:example/1','platform':'telegram'}]}
        result=commit(store,self.report(),snapshot=snapshot,return_state=True)
        self.assertEqual(result,store.read_json('collection-cursors.json'))
        original=store.read_json;reads=0
        def corrupt(name):
            nonlocal reads
            if name=='collection-cursors.json':
                reads+=1
                if reads==2:return {'corrupt':True}
            return original(name)
        with patch.object(store,'read_json',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'cursor_readback_mismatch'):
                commit(store,self.report(),snapshot=snapshot,return_state=True)


if __name__=='__main__':unittest.main()
