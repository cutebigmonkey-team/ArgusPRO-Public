import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'local-storage'))
from sample_package import create
from storage import pack,validate_package
from drive_store import DriveStore,encoded
from publish import publish,load_snapshot,monitor

class MemoryStore:
    def __init__(self): self.files={};self.private=True;self.fail=None
    def assert_private(self):
        if not self.private: raise ValueError('Not private')
    def read_json(self,name): return json.loads(self.files[name]) if name in self.files else None
    def read_bytes(self,name,limit):
        data=self.files.get(name)
        if data is not None and len(data)>limit:raise ValueError('Archive JSON is too large')
        return data
    def put(self,name,data,mutable=False):
        if self.fail and name.startswith(self.fail): raise RuntimeError('Simulated lost upload')
        if name in self.files and self.files[name]!=data and not mutable: raise ValueError('Immutable')
        self.files[name]=data
        return 'file_'+hashlib.sha256(name.encode()).hexdigest()
    def json(self,name,data,mutable=False): return self.put(name,encoded(data),mutable)

class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.package=self.root/'package.zip';create(self.package);self.store=MemoryStore()
    def test_comment_api_identity_survives_archive_normalization(self):
        with validate_package(self.package,self.root) as (_,posts,extracted):
            for p in posts:p.update(platform_post_id='12_34',conversation_id='34')
            source=self.root/'posts.jsonl';source.write_text(''.join(json.dumps(p)+'\n' for p in posts))
            package=self.root/'native.zip';pack(source,extracted/'media',package,sample=True)
        publish(self.store,package,True)
        self.assertTrue(all(p.get('platform_post_id')=='12_34' and p.get('conversation_id')=='34' for p in load_snapshot(self.store)['items']))
    def test_replay_and_media_dedup_without_database(self):
        with patch('storage.psql',side_effect=AssertionError('No local DB allowed')):
            self.assertEqual(publish(self.store,self.package,True)['status'],'imported')
            old=copy.deepcopy(self.store.files)
            self.assertEqual(publish(self.store,self.package,True)['status'],'already_imported')
            self.assertEqual(self.store.files,old)
        self.assertEqual(len(load_snapshot(self.store)['items']),2)
        self.assertEqual(len([n for n in self.store.files if n.startswith('media-')]),1)
    def test_sample_opt_in_and_public_folder_refused(self):
        with self.assertRaises(ValueError): publish(self.store,self.package)
        self.assertEqual(self.store.files,{})
        self.store.private=False
        with self.assertRaises(ValueError): publish(self.store,self.package,True)
        self.assertEqual(self.store.files,{})
    def test_failed_upload_does_not_advance_index_and_can_retry(self):
        self.store.fail='snapshot-'
        with self.assertRaises(RuntimeError): publish(self.store,self.package,True)
        self.assertNotIn('catalog.json',self.store.files)
        self.store.fail=None
        publish(self.store,self.package,True)
        self.assertEqual(len(load_snapshot(self.store)['items']),2)
        self.assertEqual(len([n for n in self.store.files if n.startswith('media-')]),1)
    def test_newer_post_wins_and_original_packages_remain(self):
        publish(self.store,self.package,True)
        with validate_package(self.package,self.root) as (_,posts,extracted):
            for p in posts: p['observed_at']='2030-01-01T00:00:00Z';p['text']='updated'
            source=self.root/'posts.jsonl';source.write_text(''.join(json.dumps(p)+'\n' for p in posts))
            newer=self.root/'new.zip';pack(source,extracted/'media',newer,sample=True)
        publish(self.store,newer,True)
        create(self.root/'old.zip');publish(self.store,self.root/'old.zip',True)
        self.assertTrue(all(p['text']=='updated' for p in load_snapshot(self.store)['items']))
        self.assertEqual(len([n for n in self.store.files if n.startswith('media-')]),1)
    def test_tiktok_analysis_retained_once_when_caption_refreshes(self):
        media = self.root / 'empty-media'
        media.mkdir()
        caption = 'Original caption'
        analysis = 'Speech from the verified clip'
        item = {'platform': 'tiktok', 'canonical_key': 'tiktok:123456789',
                'post_url':'https://www.tiktok.com/@example/video/123456789',
                'published_at': None, 'observed_at':'2026-01-01T00:00:00Z', 'media': [],
                'text': caption + '\n\n' + analysis, 'video_analysis_text': analysis}
        posts = self.root / 'posts.jsonl'
        for day in (1, 2):
            if day == 2:
                item.update(text='Updated caption', observed_at='2026-01-02T00:00:00Z')
                del item['video_analysis_text']
            posts.write_text(json.dumps(item) + '\n')
            package = self.root / f'video-{day}.zip'
            pack(posts, media, package)
            publish(self.store, package)
            saved = load_snapshot(self.store)['items'][0]
            self.assertEqual(saved['text'].count(analysis), 1)
            self.assertTrue(saved['text'].startswith(caption if day == 1 else 'Updated caption'))

    def test_tampered_catalog_snapshot_rejected(self):
        publish(self.store,self.package,True)
        catalog=self.store.read_json('catalog.json')
        self.store.files[catalog['snapshot_name']]=b'{"items":[]}'
        with self.assertRaises(ValueError): load_snapshot(self.store)

    def test_partitioned_snapshot_replay_and_interrupted_part_upload(self):
        with patch('publish.MAX_JSON',100),patch('drive_store.PART_BYTES',256):
            self.store.fail='part-'
            with self.assertRaises(RuntimeError):publish(self.store,self.package,True,measure=True)
            self.assertNotIn('catalog.json',self.store.files)
            self.store.fail=None
            result=publish(self.store,self.package,True,measure=True)
            catalog=self.store.read_json('catalog.json')
            self.assertGreater(len(catalog['snapshot_parts']),1)
            self.assertEqual(len(load_snapshot(self.store)['items']),2)
            self.assertEqual(publish(self.store,self.package,True,measure=True)['new_keys'],result['new_keys'])
            first=catalog['snapshot_parts'][0]
            self.store.files['part-'+first['sha256']+'.bin']=b'x'*first['size']
            with self.assertRaisesRegex(ValueError,'integrity'):load_snapshot(self.store)

    def test_legacy_facebook_aliases_share_one_archive_delta(self):
        media=self.root/'empty';media.mkdir()
        posts=self.root/'observations.jsonl'
        item={'platform':'facebook','canonical_key':'facebook:pfbidABC','facebook_story_id':'55',
              'post_url':'https://www.facebook.com/example/posts/pfbidABC','text':'Original body','published_at':'2026-01-01T00:00:00Z',
              'text_may_be_truncated':False,'observed_at':'2026-01-02T00:00:00Z','media':[]}
        for i in range(2):
            if i:
                item.update(canonical_key='facebook:55',post_url='https://www.facebook.com/example/posts/55',
                            published_at=None,text='Original…',text_may_be_truncated=True,observed_at='2026-01-03T00:00:00Z')
            posts.write_text(json.dumps(item)+'\n',encoding='utf-8')
            archive=self.root/f'alias-{i}.zip';pack(posts,media,archive)
            new=publish(self.store,archive,measure=True)['new_keys']
            self.assertEqual(new,[] if i else ['facebook:pfbidABC'])
            self.assertEqual(publish(self.store,archive,measure=True)['new_keys'],new)
        saved,=load_snapshot(self.store)['items']
        self.assertEqual((saved['id'],saved['text']),('facebook:pfbidABC','Original body'))
        self.assertTrue(saved['published_at'])
        self.assertIn('facebook:55',saved['identity_aliases'])
    def test_monitor_whitelists_metrics_and_omits_sensitive_details(self):
        p=self.root/'yt-dlp/result.json';p.parent.mkdir();p.write_text(json.dumps({'status':'passed','error':'secret',
            'validation':{'source_url':'private'},'capture':{'wall_seconds':1.2,'max_process_rss_kib':100,'token':'secret'}}))
        monitor(self.store,self.root,'passed')
        report=self.store.files['monitor.json'].decode()
        self.assertNotIn('secret',report);self.assertNotIn('source_url',report)
        self.assertEqual(self.store.read_json('monitor.json')['stages'][0]['metrics'][0]['wall_seconds'],1.2)

class Request:
    def __init__(self,data): self.data=data
    def execute(self,**kwargs): return self.data
class Service:
    def __init__(self,permission='user'): self.permission=permission;self.query=None
    def files(self): return self
    def permissions(self): return self
    def get(self,**kwargs): return Request({'mimeType':'application/vnd.google-apps.folder','capabilities':{'canAddChildren':True}})
    def list(self,**kwargs):
        if 'q' in kwargs: self.query=kwargs['q'];return Request({'files':[]})
        return Request({'permissions':[{'type':self.permission,'role':'reader'}]})
class DriveBoundaryTests(unittest.TestCase):
    def test_shifted_snapshot_reuses_verified_immutable_parts(self):
        from drive_store import reuse_snapshot_parts, write_parts
        store = MemoryStore()
        with patch('drive_store.PART_BYTES', 8):
            old = b'111111112222222233333333'
            parts = write_parts(store,old)
            updated = b'0000' + old + b'4444'
            with patch.object(store,'put',wraps=store.put) as put:
                result = reuse_snapshot_parts(store,updated,old,parts)
            self.assertEqual(result[1:-1],parts)
            self.assertEqual(put.call_count,2)
            self.assertEqual(b''.join(store.files['part-'+p['sha256']+'.bin'] for p in result),updated)
    def test_large_platform_view_and_legacy_view_use_the_same_reader(self):
        memory=MemoryStore()
        store=DriveStore(Service(),'test_root_123')
        with patch.object(store,'read_bytes',side_effect=memory.read_bytes),patch.object(store,'put',side_effect=memory.put),patch('drive_store.MAX_JSON',1024),patch('drive_store.PART_BYTES',768):
            large={'posts':[{'text':'Допомога'*200}]}
            store.json('posts.json',large,mutable=True)
            self.assertGreater(len(memory.read_json('posts.json')['parts']),1)
            self.assertEqual(store.read_json('posts.json'),large)
            name='package-'+'a'*64+'.json'
            store.json(name,large)
            self.assertEqual(store.read_json(name),large)
            store.json('posts.json',{'posts':[]},mutable=True)
            self.assertEqual(store.read_json('posts.json'),{'posts':[]})
    def test_permissions_and_queries_are_scoped(self):
        for permission in ('anyone','domain','group'):
            with self.assertRaises(ValueError): DriveStore(Service(permission),'test_root_123').assert_private()
        service=Service();store=DriveStore(service,'test_root_123');store.assert_private();store.find('catalog.json')
        self.assertIn("'test_root_123' in parents",service.query)
        with self.assertRaises(ValueError): store.find("x' or trashed=true")

if __name__=='__main__': unittest.main()
