import copy
import unittest
from test_publish import MemoryStore
from publish_hourly import publish_delta


class HourlyPublicationTests(unittest.TestCase):
    def test_delayed_package_preserves_first_discovery_and_latest_body(self):
        store = MemoryStore()
        publish_delta(store,[self.post(text='Newer',observed_at='2026-10-31T11:00:00Z')])
        from publish import publish, load_snapshot
        import tempfile, json
        from pathlib import Path
        from storage import pack
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'media').mkdir()
            (root/'posts').write_text(json.dumps(self.post(text='Older')))
            pack(root/'posts',root/'media',root/'pkg')
            publish(store,root/'pkg')
        row, = load_snapshot(store)['items']
        self.assertEqual(row['discovered_at'],'2026-10-30T10:00:00+00:00')
        self.assertEqual(row['collected_at'],'2026-10-31T11:00:00+00:00')
        self.assertEqual(row['text'],'Newer')

    def post(self, **extra):
        return {'canonical_key':'telegram:example/1', 'platform':'telegram', 'post_url':'https://t.me/example/1',
                'text':'Real post', 'published_at':'2026-10-30T09:00:00Z', 'observed_at':'2026-10-30T10:00:00Z',
                'media':[], 'media_urls':[], 'source_name':'Example', **extra}

    def test_next_hour_without_content_changes_does_not_grow_archive(self):
        store = MemoryStore()
        _, result, _ = publish_delta(store, [self.post()])
        self.assertEqual(result['new_posts'], 1); self.assertEqual(result['replay'], 'unchanged')
        previous = copy.deepcopy(store.files)
        _, result, _ = publish_delta(store, [self.post(observed_at='2026-10-30T11:00:00Z')])
        self.assertEqual(result['new_posts'], 0); self.assertEqual(result['unchanged_posts'], 1)
        self.assertEqual(store.files, previous)

    def test_edited_post_updates_same_identity_and_preserves_unrelated_posts(self):
        store = MemoryStore()
        publish_delta(store, [self.post(), self.post(canonical_key='telegram:example/2', post_url='https://t.me/example/2')])
        snapshot, result, _ = publish_delta(store, [self.post(text='Edited', observed_at='2026-10-30T11:00:00Z')])
        self.assertEqual(result['new_posts'], 0); self.assertEqual(result['updated_posts'], 1)
        self.assertEqual(len(snapshot['items']), 2)

    def test_new_anchor_metadata_is_saved_even_when_body_is_unchanged(self):
        store=MemoryStore(); publish_delta(store,[self.post()])
        updated=self.post(observed_at='2026-10-30T11:00:00Z',outbound_social_links=['https://www.instagram.com/p/ABCDE/'])
        snapshot,result,_=publish_delta(store,[updated])
        self.assertEqual(result['updated_posts'],1)
        self.assertEqual(snapshot['items'][0]['outbound_social_links'],updated['outbound_social_links'])


if __name__ == '__main__': unittest.main()
