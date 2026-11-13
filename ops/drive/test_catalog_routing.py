"""Exercise the real publisher and reclassification of a legacy multipart snapshot."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from publish import commit_snapshot, encoded, load_snapshot, publish
from reclassify import reclassify
from source_origin import SourceOrigins
from storage import pack
from test_publish import MemoryStore


class CatalogRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        rows = json.loads((Path(__file__).resolve().parents[1]/'collect/sources.json').read_bytes())['sources']
        self.sources = [next(s for s in rows if s['platform']==platform and s['origin']==origin)
                        for platform,origin in [('facebook','dsns'),('telegram','dsns'),('rss','dsns'),
                                                ('rss','general'),('telegram','general')]]
        self.store.json('sources.json', {'sources':self.sources})
        self.posts = [{'canonical_key':s['platform']+':'+str(i), 'platform':s['platform'],
                       'post_url':s['url'].rstrip('/')+'/42', 'text':'ДСНС: повідомлення з медіа',
                       'observed_at':'2026-10-29T12:00:00Z','published_at':'2026-10-29T10:00:00Z',
                       'media':[], 'media_urls':['https://media.example/photo'],
                       'source_id':s['id'],'source_url':s['url'],'source_name':s['name']}
                      for i,s in enumerate(self.sources)]
        self.posts.append({**self.posts[1], 'canonical_key':'telegram:other/42',
                           'source_id':'other','source_url':'https://t.me/other','source_name':'ДСНС'})
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root/'media').mkdir()
        path=root/'posts.jsonl'
        path.write_text(''.join(json.dumps(p)+'\n' for p in self.posts))
        self.package=root/'package.zip'
        pack(path,root/'media',self.package)

    def test_new_posts_and_repeated_import(self):
        publish(self.store,self.package)
        self.assertEqual(publish(self.store,self.package)['status'],'already_imported')
        origins={p['id']:p['origin'] for p in load_snapshot(self.store)['items']}
        self.assertEqual([origins[p['canonical_key']] for p in self.posts],
                         ['dsns','dsns','dsns','general','general','unknown'])
        self.assertEqual(len(origins),len(self.posts))

    def test_first_package_from_reviewed_social_profiles_before_catalog_refresh(self):
        from source_origin import catalog_origins
        from import_sheet import preserve_verified_profiles
        catalog=json.loads((Path(__file__).resolve().parents[1]/'collect/sources.json').read_bytes())
        profiles=[s for s in catalog['sources'] if any(r.get('kind')=='verified_public_profile'
                                                       for r in s.get('references',[]))]
        old={'sources':[s for s in catalog['sources'] if s not in profiles]}
        self.store.json('sources.json',old,mutable=True)
        lookup=catalog_origins(self.store)
        kinds={(s['platform'],r['owner_kind']) for s in profiles for r in s['references'] if r.get('owner_kind')}
        self.assertEqual({p for p,k in kinds if k=='media'},{'facebook','instagram','x','youtube','threads','tiktok'})
        self.assertTrue({('facebook','politician'),('instagram','politician')}.issubset(kinds))
        for s in profiles:
            self.assertEqual(lookup.resolve({'platform':s['platform'],'source_id':s['id'],'source_url':s['url']}),'general')
        regenerated=preserve_verified_profiles(old,catalog)
        self.assertEqual({s['id']:s for s in regenerated['sources']},{s['id']:s for s in catalog['sources']})
        self.assertEqual(preserve_verified_profiles(regenerated,catalog),regenerated)
        catalog_origins(self.store,register=True)
        updated=self.store.read_json('sources.json')
        self.assertEqual(updated['sources'],old['sources']+profiles)
        catalog_origins(self.store,register=True)
        self.assertEqual(updated,self.store.read_json('sources.json'))

    def test_operator_bookmarks_register_once_and_survive_workbook_reimport(self):
        from import_sheet import is_operator_source, preserve_verified_profiles
        from source_origin import catalog_origins
        shipped=json.loads((Path(__file__).resolve().parents[1]/'collect/sources.json').read_bytes())
        requested=[s for s in shipped['sources'] if is_operator_source(s)]
        original={'sources':[s for s in shipped['sources'] if not is_operator_source(s)]}
        self.assertTrue(requested)
        self.store.json('sources.json', original, mutable=True)
        catalog_origins(self.store,register=True)
        saved=self.store.read_json('sources.json')
        self.assertEqual(saved['sources'],original['sources']+requested)
        catalog_origins(self.store,register=True)
        self.assertEqual(self.store.read_json('sources.json'),saved)
        self.assertEqual({s['id']:s for s in preserve_verified_profiles(original,shipped)['sources']},{s['id']:s for s in shipped['sources']})

    def test_directory_profiles_register_without_claiming_individual_verification(self):
        from import_sheet import is_verified_profile, preserve_verified_profiles
        from source_origin import catalog_origins
        shipped=json.loads((Path(__file__).resolve().parents[1]/'collect/sources.json').read_bytes())
        profiles=[s for s in shipped['sources'] if any(r.get('kind')=='wikidata_instagram'
                                                       for r in s['references'])]
        self.assertTrue(profiles)
        self.assertFalse(any(is_verified_profile(s) for s in profiles))
        original={'sources':[s for s in shipped['sources'] if s not in profiles]}
        self.store.json('sources.json',original,mutable=True)
        catalog_origins(self.store,register=True)
        catalog_origins(self.store,register=True)
        self.assertEqual(self.store.read_json('sources.json')['sources'],original['sources']+profiles)
        self.assertEqual({s['id']:s for s in preserve_verified_profiles(original,shipped)['sources']},{s['id']:s for s in shipped['sources']})

    def test_multipart_legacy_reclassification_preserves_every_other_field_and_is_idempotent(self):
        with patch('publish.MAX_JSON', 100), patch('drive_store.PART_BYTES', 512):
            publish(self.store,self.package)
            before=load_snapshot(self.store)
            for row in before['items']:
                for key in ('origin','source_id','source_url'):
                    row.pop(key, None)
            commit_snapshot(self.store,before,encoded(before))
            pointer=self.store.read_json('catalog.json')
            self.assertGreater(len(pointer['snapshot_parts']),1)
            self.assertEqual(reclassify(self.store)['changed_records'],6)
            self.assertEqual(self.store.read_json('catalog.json'),pointer)
            self.assertEqual(reclassify(self.store,apply=True)['changes'],{'dsns':3,'general':2,'unknown':1})
            after=load_snapshot(self.store)
            for row in after['items']:
                row.pop('origin')
            self.assertEqual(before['items'],after['items'])
            self.assertEqual(before['packages'],after['packages'])
            done=self.store.read_json('catalog.json')
            self.assertEqual(reclassify(self.store,apply=True)['changed_records'],0)
            self.assertEqual(self.store.read_json('catalog.json'),done)

    def test_source_id_platform_url_conflicts_and_search_previews(self):
        origins=SourceOrigins(self.sources)
        row=self.posts[1]
        self.assertEqual(origins.resolve({**row,'source_id':None}), 'dsns')
        self.assertEqual(origins.resolve({**row,'source_url':self.sources[-1]['url']}),'unknown')
        self.assertEqual(origins.resolve({**row,'platform':'facebook'}),'unknown')
        self.assertEqual(origins.resolve({**row,'source_match':'search_query_only'}),'unknown')
